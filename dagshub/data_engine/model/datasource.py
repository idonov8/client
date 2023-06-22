import base64
import gzip
import json
import logging
import math
import os.path
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING, Union, Iterator

import httpx
import requests
import rich.progress
from dataclasses_json import dataclass_json
from pathvalidate import sanitize_filepath

import dagshub.auth
from dagshub.auth.token_auth import HTTPBearerAuth
from dagshub.common import config
from dagshub.common.helpers import sizeof_fmt, prompt_user, http_request
from dagshub.common.rich_util import get_rich_progress
from dagshub.common.util import lazy_load
from dagshub.data_engine.client.models import PreprocessingStatus, Datapoint
from dagshub.data_engine.model.errors import WrongOperatorError, WrongOrderError, DatasetFieldComparisonError
from dagshub.data_engine.model.query import DatasourceQuery, _metadataTypeLookup
from dagshub.data_engine.voxel_plugin_server.utils import set_voxel_envvars

if TYPE_CHECKING:
    from dagshub.data_engine.model.datasources import DatasourceState
    from dagshub.data_engine.client.data_client import QueryResult
    import fiftyone as fo
    import pandas
    import dagshub.data_engine.voxel_plugin_server.server as plugin_server_module
else:
    plugin_server_module = lazy_load("dagshub.data_engine.voxel_plugin_server.server")
    fo = lazy_load("fiftyone")

logger = logging.getLogger(__name__)


@dataclass_json
@dataclass
class DatapointMetadataUpdateEntry(json.JSONEncoder):
    url: str
    key: str
    value: str
    valueType: str


class Datasource:

    def __init__(self, datasource: "DatasourceState", query: Optional[DatasourceQuery] = None):
        self._source = datasource
        if query is None:
            query = DatasourceQuery()
        self._query = query

        self.serialize_gql_query_input()

    @property
    def source(self):
        return self._source

    def clear_query(self):
        """
        This function clears the query assigned to this datasource.
        Once you clear the query, next time you try to get datapoints, you'll get all the datapoints in the datasource
        """
        self._query = DatasourceQuery()

    def __deepcopy__(self, memodict={}) -> "Datasource":
        res = Datasource(self._source, self._query.__deepcopy__())
        return res

    def get_query(self):
        return self._query

    @property
    def annotation_columns(self) -> List[str]:
        # TODO: once the annotation type is implemented, expose those columns here
        return ["annotation"]

    def serialize_gql_query_input(self):
        return {
            "query": self._query.serialize_graphql(),
        }

    def sample(self, start: Optional[int] = None, end: Optional[int] = None):
        if start is not None:
            logger.warning("Starting slices is not implemented for now")
        return self._source.client.sample(self, end, include_metadata=True)

    def head(self) -> "QueryResult":
        self._check_preprocess()
        return self._source.client.head(self)

    def all(self) -> "QueryResult":
        self._check_preprocess()
        return self._source.client.get_datapoints(self)

    def _check_preprocess(self):
        self.source.get_from_dagshub()
        if self.source.preprocessing_status == PreprocessingStatus.IN_PROGRESS:
            logger.warning(
                f"Datasource {self.source.name} is currently in the progress of rescanning. "
                f"Values might change if you requery later")

    @contextmanager
    def metadata_context(self) -> Iterator["MetadataContextManager"]:
        """
        Returns a metadata context, that you can upload metadata through via update_metadata
        Once the context is exited, all metadata is uploaded in one batch

        with df.metadata_context() as ctx:
            ctx.update_metadata(["file1", "file2"], {"key1": True, "key2": "value"})

        """
        ctx = MetadataContextManager(self)
        yield ctx
        self._upload_metadata(ctx.get_metadata_entries())

    def upload_metadata_from_dataframe(self, df: "pandas.DataFrame", path_column: Optional[Union[str, int]] = None):
        """
        Uploads metadata from a pandas dataframe
        path_column can either be a name of the column with the data or its index.
        This will be the column from which the datapoints are extracted.
        All the other columns are treated as metadata to upload
        If path_column is not specified, the first column is used as the datapoints
        """
        self._upload_metadata(self._df_to_metadata(df, path_column))

    @staticmethod
    def _df_to_metadata(df: "pandas.DataFrame", path_column: Optional[Union[str, int]] = None) -> List[
        DatapointMetadataUpdateEntry]:
        res = []
        if path_column is None:
            path_column = df.columns[0]
        elif type(path_column) is str:
            if path_column not in df.columns:
                raise RuntimeError(f"Column {path_column} does not exist in the dataframe")
        elif type(path_column) is int:
            path_column = df.columns[path_column]

        # objects are actually mixed and not guaranteed to be string, but this should cover most use cases
        if df.dtypes[path_column] != "object":
            raise RuntimeError(f"Column {path_column} doesn't have strings")

        for _, row in df.iterrows():
            datapoint = row[path_column]
            for key, val in row.items():
                if key == path_column:
                    continue
                if val is None:
                    continue
                if type(val) is float and math.isnan(val):
                    continue
                res.append(DatapointMetadataUpdateEntry(
                    url=datapoint,
                    key=str(key),
                    value=str(val),
                    valueType=_metadataTypeLookup[type(val)]
                ))
        return res

    def delete_source(self, force: bool = False):
        """
        Delete the record of this datasource
        This will remove ALL the datapoints + metadata associated with the datasource
        """
        prompt = f"You are about to delete datasource \"{self.source.name}\" for repo \"{self.source.repo}\"\n" \
                 f"This will remove the datasource and ALL datapoints " \
                 f"and metadata records associated with the source."
        if not force:
            user_response = prompt_user(prompt)
            if not user_response:
                print("Deletion cancelled")
                return
        self.source.client.delete_datasource(self)

    def generate_datapoints(self):
        """
        This function fires a call to the backend to rescan the datapoints.
        Call this function whenever you updated/new files and want the changes to show up in the datasource metadata
        """
        logger.debug("Rescanning datasource")
        self.source.client.scan_datasource(self)

    def _upload_metadata(self, metadata_entries: List[DatapointMetadataUpdateEntry]):

        progress = get_rich_progress(rich.progress.MofNCompleteColumn())

        upload_batch_size = 15000
        total_entries = len(metadata_entries)
        total_task = progress.add_task(f"Uploading metadata (batch size {upload_batch_size})...",
                                       total=total_entries)

        with progress:
            for start in range(0, total_entries, upload_batch_size):
                entries = metadata_entries[start:start + upload_batch_size]
                logger.debug(f"Uploading {len(entries)} metadata entries...")
                self.source.client.update_metadata(self, entries)
                progress.update(total_task, advance=upload_batch_size)
            progress.update(total_task, completed=total_entries, refresh=True)

    def __str__(self):
        return f"<Dataset source:{self._source}, query: {self._query}>"

    def save_dataset(self, name: str):
        """
        Save the dataset, which is a combination of datasource + query, on the backend.
        That way you can persist and share your queries on the backend
        You can get the dataset back by calling `datasources.get_dataset(repo, name)`
        """
        self.source.client.save_dataset(self, name)

    def to_voxel51_dataset(self, **kwargs) -> "fo.Dataset":
        """
        Creates a voxel51 dataset that can be used with `fo.launch_app()` to run it

        Args:
            name (str): name of the dataset (by default uses the same name as the datasource)
            force_download (bool): download the dataset even if the size of the files is bigger than 100MB
            files_location (str|PathLike): path to the location where to download the local files
                default: ~/dagshub_datasets/user/repo/ds_name/
        """
        logger.info("Migrating dataset to voxel51")
        name = kwargs.get("name", self._source.name)
        force_download = kwargs.get("force_download", False)
        # TODO: don't override samples in existing one
        if fo.dataset_exists(name):
            ds: fo.Dataset = fo.load_dataset(name)
        else:
            ds: fo.Dataset = fo.Dataset(name)
        # ds.persistent = True
        dataset_location = kwargs.get("files_location", sanitize_filepath(
            os.path.join(Path.home(), "dagshub_datasets", self.source.repo, self.source.name)))

        os.makedirs(dataset_location, exist_ok=True)
        logger.info("Downloading files...")

        # Load the dataset from the query
        datapoints = self.all().download_binary_columns(*self.annotation_columns)

        if not force_download:
            self._check_downloaded_dataset_size(datapoints)

        host = config.host
        client = httpx.Client(auth=HTTPBearerAuth(dagshub.auth.get_token(host=host), ), follow_redirects=True)

        logger.warning(f"Downloading {len(datapoints.entries)} files to {dataset_location}")
        samples = []

        progress = get_rich_progress(rich.progress.MofNCompleteColumn())
        task = progress.add_task("Creating voxel dataset...", total=len(datapoints.entries))

        with progress:
            # TODO: parallelize this with some async magic
            for datapoint in datapoints.entries:
                file_url = datapoint.download_url(self)
                resp = client.get(file_url, follow_redirects=True)
                try:
                    assert resp.status_code == 200
                except AssertionError:
                    logger.warning(
                        f"Couldn't get image for path {datapoint.path}. Response code {resp.status_code} (Body: {resp.content})")
                    continue
                # TODO: doesn't work with nesting
                filename = file_url.split("/")[-1]
                filepath = os.path.join(dataset_location, filename)
                with open(filepath, "wb") as f:
                    f.write(resp.content)
                sample = fo.Sample(filepath=filepath)
                sample["dagshub_download_url"] = file_url
                self._handle_ls_annotation(sample, datapoint, "annotation")
                for k, v in datapoint.metadata.items():
                    if type(v) is not bytes:
                        sample[k] = v
                samples.append(sample)
                progress.update(task, advance=1)
        progress.update(task, completed=len(datapoints.entries), refresh=True)

        logger.info(f"Downloaded {len(datapoints.dataframe['name'])} file(s) into {dataset_location}")
        ds.add_samples(samples)

        ds.compute_metadata(skip_failures=True, overwrite=True)

        return ds

    @staticmethod
    def _handle_ls_annotation(sample: "fo.Sample", datapoint: "Datapoint", *annotation_fields: str):
        from fiftyone.utils.labelstudio import import_label_studio_annotation
        # if datapoint.path == "backyard_squirrels_000028.jpg":
        #     print(datapoint.metadata)
        for field in annotation_fields:
            annotations = datapoint.metadata.get(field)
            if type(annotations) is not bytes:
                return
            ann_dict = json.loads(annotations.decode())
            for ann in ann_dict["annotations"]:
                if "result" not in ann:
                    continue
                for res in ann["result"]:
                    try:
                        converted = import_label_studio_annotation(res)
                        sample.add_labels(converted, label_field=field)
                    except:
                        logger.warning(f"Couldn't convert LS annotation {res} to voxel annotation")

    def visualize(self, **kwargs):
        set_voxel_envvars()

        ds = self.to_voxel51_dataset(**kwargs)

        sess = fo.launch_app(ds)
        # Launch the server for plugin interaction
        plugin_server_module.run_plugin_server(sess, self, self.source.revision)

        return sess

    @staticmethod
    def _check_downloaded_dataset_size(datapoints: "QueryResult"):
        download_size_prompt_threshold = 100 * (2 ** 20)  # 100 Megabytes
        dp_size = Datasource._calculate_datapoint_size(datapoints)
        if dp_size is not None and dp_size > download_size_prompt_threshold:
            prompt = f"You're about to download {sizeof_fmt(dp_size)} of images locally."
            should_download = prompt_user(prompt)
            if not should_download:
                msg = "Downloading voxel dataset cancelled"
                logger.warning(msg)
                raise RuntimeError(msg)

    @staticmethod
    def _calculate_datapoint_size(datapoints: "QueryResult") -> Optional[int]:
        sum_size = 0
        has_sum_field = False
        all_have_sum_field = True
        size_field = "size"
        for dp in datapoints.entries:
            if size_field in dp.metadata:
                has_sum_field = True
                sum_size += dp.metadata[size_field]
            else:
                all_have_sum_field = False
        if not has_sum_field:
            logger.warning("None of the datapoints had a size field, can't calculate size of the downloading dataset")
            return None
        if not all_have_sum_field:
            logger.warning("Not every datapoint has a size field, size calculations might be wrong")
        return sum_size

    def _send_to_annotation(self, url: str):
        """ TEMP FUNCTION """
        auth = HTTPBearerAuth(dagshub.auth.get_token(host=self.source.client.host))

        def _http_request(method, url, **kwargs):
            if "auth" not in kwargs:
                kwargs["auth"] = auth
            return http_request(method, url, **kwargs)

        dps = self.all()

        data = {"datasourceid": str(self.source.id),
                "datapoints": [{"id": str(dp.datapoint_id),
                                "downloadurl": dp.download_url(self)[:7] + dp.download_url(self)[7:].replace('//', '/')}
                               for dp in dps.entries]}

        logger.debug(f"Sending request to URL {url}\nwith data: {data}")

        resp = _http_request("POST", url, json=data)

        print(resp)
        if resp.status_code == 200:
            print(resp.json()['link'])
        return

    """ FUNCTIONS RELATED TO QUERYING
    These are functions that overload operators on the DataSet, so you can do pandas-like filtering
        ds = Dataset(...)
        queried_ds = ds[ds["value"] == 5]
    """

    def __getitem__(self, other: Union[slice, str, "Datasource"]):
        # Slicing - get items from the slice
        if type(other) is slice:
            return self.sample(other.start, other.stop)

        # Otherwise we're doing querying
        new_ds = self.__deepcopy__()
        if type(other) is str:
            new_ds._query = DatasourceQuery(other)
            return new_ds
        else:
            # "index" is a dataset with a query - compose with "and"
            # Example:
            #   ds = Dataset()
            #   filtered_ds = ds[ds["aaa"] > 5]
            #   filtered_ds2 = filtered_ds[filtered_ds["bbb"] < 4]
            if self._query.is_empty:
                new_ds._query = other._query
                return new_ds
            else:
                return other.__and__(self)

    def __gt__(self, other: object):
        self._test_not_comparing_other_ds(other)
        if not isinstance(other, (int, float, str)):
            raise NotImplementedError
        return self.add_query_op("gt", other)

    def __ge__(self, other: object):
        self._test_not_comparing_other_ds(other)
        if not isinstance(other, (int, float, str)):
            raise NotImplementedError
        return self.add_query_op("ge", other)

    def __le__(self, other: object):
        self._test_not_comparing_other_ds(other)
        if not isinstance(other, (int, float, str)):
            raise NotImplementedError
        return self.add_query_op("le", other)

    def __lt__(self, other: object):
        self._test_not_comparing_other_ds(other)
        if not isinstance(other, (int, float, str)):
            raise NotImplementedError
        return self.add_query_op("lt", other)

    def __eq__(self, other: object):
        self._test_not_comparing_other_ds(other)
        if not isinstance(other, (int, float, str)):
            raise NotImplementedError
        return self.add_query_op("eq", other)

    def __ne__(self, other: object):
        self._test_not_comparing_other_ds(other)
        if not isinstance(other, (int, float, str)):
            raise NotImplementedError
        return self.add_query_op("ne", other)

    def __contains__(self, item):
        raise WrongOperatorError("Use `ds.contains(a)` for querying instead of `a in ds`")

    def contains(self, item: str):
        if type(item) is not str:
            return WrongOperatorError(f"Cannot use contains with non-string value {item}")
        self._test_not_comparing_other_ds(item)
        return self.add_query_op("contains", item)

    def __and__(self, other: "Datasource"):
        return self.add_query_op("and", other)

    def __or__(self, other: "Datasource"):
        return self.add_query_op("or", other)

    # Prevent users from messing up their queries due to operator order
    # They always need to put the dataset query filters in parentheses, otherwise the binary and/or get executed before
    def __rand__(self, other):
        if type(other) is not Datasource:
            raise WrongOrderError(type(other))
        raise NotImplementedError

    def __ror__(self, other):
        if type(other) is not Datasource:
            raise WrongOrderError(type(other))
        raise NotImplementedError

    def add_query_op(self, op: str, other: Union[str, int, float, "Datasource", "DatasourceQuery"]) -> "Datasource":
        """
        Returns a new dataset with an added query param
        """
        new_ds = self.__deepcopy__()
        if type(other) is Datasource:
            other = other.get_query()
        new_ds._query.compose(op, other)
        return new_ds

    @staticmethod
    def _test_not_comparing_other_ds(other):
        if type(other) is Datasource:
            raise DatasetFieldComparisonError()


class MetadataContextManager:
    def __init__(self, dataset: Datasource):
        self._dataset = dataset
        self._metadata_entries: List[DatapointMetadataUpdateEntry] = []

    def update_metadata(self, datapoints: Union[List[str], str], metadata: Dict[str, Any]):
        if isinstance(datapoints, str):
            datapoints = [datapoints]
        for dp in datapoints:
            for k, v in metadata.items():
                if v is None:
                    continue
                if type(v) is float and math.isnan(v):
                    continue
                value_type = _metadataTypeLookup[type(v)]
                if type(v) is bytes:
                    v = self._wrap_bytes(v)
                self._metadata_entries.append(DatapointMetadataUpdateEntry(
                    url=dp,
                    key=k,
                    value=str(v),
                    # todo: preliminary type check
                    valueType=value_type,
                ))

    @staticmethod
    def _wrap_bytes(val: bytes) -> str:
        """
        Handles bytes values for uploading metadata
        The process is gzip -> base64
        """
        compressed = gzip.compress(val)
        return base64.b64encode(compressed).decode("utf-8")

    def get_metadata_entries(self):
        return self._metadata_entries