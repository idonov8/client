import enum
import logging
import multiprocessing.pool
from dataclasses import dataclass
from itertools import repeat
from typing import Dict, Any, List, Union, TYPE_CHECKING, Optional, Tuple

from dagshub.common.helpers import http_request

if TYPE_CHECKING:
    from dagshub.data_engine.model.datasource import Datasource

logger = logging.getLogger(__name__)


@dataclass
class Metadata:
    key: str
    value: Any


@dataclass
class Datapoint:
    datapoint_id: str
    path: str
    metadata: Dict[str, Any]

    def download_url(self, ds: "Datasource"):
        return ds.source.raw_path(self)

    def path_in_repo(self, ds: "Datasource"):
        return ds.source.file_path(self)

    @staticmethod
    def from_gql_edge(edge: Dict) -> "Datapoint":
        res = Datapoint(
            datapoint_id=edge["node"]["id"],
            path=edge["node"]["path"],
            metadata={}
        )
        for meta_dict in edge["node"]["metadata"]:
            res.metadata[meta_dict["key"]] = meta_dict["value"]
        return res

    def to_dict(self, ds: "Datasource", metadata_keys: List[str]) -> Dict[str, Any]:
        res_dict = {"name": self.path, "datapoint_id": self.datapoint_id, "dagshub_download_url": self.download_url(ds)}
        res_dict.update({key: self.metadata.get(key) for key in metadata_keys})
        return res_dict


class IntegrationStatus(enum.Enum):
    VALID = "VALID"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    MISSING = "MISSING"


class PreprocessingStatus(enum.Enum):
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    UNKNOWN = ""  # TODO: delete once it's returned consistently


class DatasourceType(enum.Enum):
    BUCKET = "BUCKET"
    REPOSITORY = "REPOSITORY"
    CUSTOM = "CUSTOM"


class MetadataFieldType(enum.Enum):
    BOOLEAN = "BOOLEAN"
    INTEGER = "INTEGER"
    FLOAT = "FLOAT"
    STRING = "STRING"
    BLOB = "BLOB"


@dataclass
class MetadataFieldSchema:
    name: str
    valueType: MetadataFieldType
    multiple: bool


@dataclass
class DatasourceResult:
    id: Union[str, int]
    name: str
    rootUrl: str
    integrationStatus: IntegrationStatus
    preprocessingStatus: PreprocessingStatus
    type: DatasourceType
    metadataFields: Optional[List[MetadataFieldSchema]]


@dataclass
class DatasetResult:
    id: Union[str, int]
    name: str
    datasource: DatasourceResult
    datasetQuery: str


@dataclass
class QueryResult:
    entries: List[Datapoint]
    """ List of downloaded entries."""
    datasource: "Datasource"

    @property
    def dataframe(self):
        import pandas as pd
        metadata_keys = set()
        for e in self.entries:
            metadata_keys.update(e.metadata.keys())

        metadata_keys = list(sorted(metadata_keys))
        return pd.DataFrame.from_records([dp.to_dict(self.datasource, metadata_keys) for dp in self.entries])

    @staticmethod
    def from_gql_query(query_resp: Dict[str, Any], datasource: "Datasource") -> "QueryResult":
        if "edges" not in query_resp:
            return QueryResult([], datasource)
        if query_resp["edges"] is None:
            return QueryResult([], datasource)
        return QueryResult([Datapoint.from_gql_edge(edge) for edge in query_resp["edges"]], datasource)

    def download_binary_columns(self, *columns: str, num_proc: int = 32) -> "QueryResult":
        """
        Downloads data from binary-defined columns
        """
        for column in columns:
            logger.info(f"Downloading metadata for column {column} with {num_proc} processes")

            def extract_blob_url(datapoint: Datapoint, col: str) -> Optional[str]:
                sha = datapoint.metadata.get(col)
                if sha is None or type(sha) is not str:
                    return None
                return self.datasource.source.blob_path(sha)

            blob_urls = map(lambda dp: extract_blob_url(dp, column), self.entries)
            auth = self.datasource.source.repoApi.auth
            func_args = zip(blob_urls, repeat(auth))
            with multiprocessing.pool.ThreadPool(num_proc) as pool:
                res = pool.starmap(_get_blob, func_args)

            for dp, binary_val in zip(self.entries, res):
                if binary_val is None:
                    continue
                dp.metadata[column] = binary_val

        return self


def _get_blob(url: Optional[str], auth) -> Optional[Union[str, bytes]]:
    if url is None:
        return None
    try:
        resp = http_request("GET", url, auth=auth)
        if resp.status_code >= 400:
            return f"Error while downloading binary blob: {resp.content.decode()}"
        else:
            return resp.content
    except Exception as e:
        return f"Error while downloading binary blob: {e}"
