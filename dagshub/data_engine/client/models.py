import enum
import logging
from dataclasses import dataclass, field
from typing import Any, List, Union, Optional, Set

from dataclasses_json import config, DataClassJsonMixin
from dagshub.data_engine.dtypes import MetadataFieldType, ReservedTags

logger = logging.getLogger(__name__)


@dataclass
class Metadata:
    key: str
    value: Any


autogenerated_columns = {
    "path",
    "datapoint_id",
    "dagshub_download_url",
}


class IntegrationStatus(enum.Enum):
    VALID = "VALID"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    MISSING = "MISSING"


class PreprocessingStatus(enum.Enum):
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    AUTO_SCAN_IN_PROGRESS = "AUTO_SCAN_IN_PROGRESS"
    FAILED = "FAILED"


class DatasourceType(enum.Enum):
    BUCKET = "BUCKET"
    REPOSITORY = "REPOSITORY"
    CUSTOM = "CUSTOM"


class ScanOption(str, enum.Enum):
    """
    Enum of options that can be applied during scanning process with
    :func:`~dagshub.data_engine.model.datasource.Datasource.scan_source`
    """

    FORCE_REGENERATE_AUTO_SCAN_VALUES = "FORCE_REGENERATE_AUTO_SCAN_VALUES"
    """
    Regenerate all the autogenerated metadata values for the whole datasource
    """


@dataclass
class MetadataFieldSchema(DataClassJsonMixin):
    # This should match the GraphQL schema: MetadataFieldProps
    name: str
    valueType: MetadataFieldType = field(metadata=config(encoder=lambda val: val.value))
    multiple: bool
    tags: Optional[Set[str]]

    def __repr__(self):
        res = f"{self.name} ({self.valueType.value})"
        if self.tags is not None and len(self.tags) > 0:
            res += f" with tags: {self.tags}"
        return res

    def is_annotation(self):
        return ReservedTags.ANNOTATION.value in self.tags if self.tags else False

    def is_document(self):
        return ReservedTags.DOCUMENT.value in self.tags if self.tags else False


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
