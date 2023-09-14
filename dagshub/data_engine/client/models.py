import enum
import logging
from dataclasses import dataclass, field
from typing import Any, List, Union, Optional
from ..dtypes import DagshubDataType, MetadataFieldType

from dataclasses_json import dataclass_json, config

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


@dataclass_json
@dataclass
class MetadataFieldSchema:
    # This should match the GraphQL schema: MetadataFieldProps
    name: str
    valueType: MetadataFieldType = field(
        metadata=config(
            encoder=lambda val: val.value
        )
    )
    multiple: bool
    tags: Optional[List[str]]

    def __repr__(self):
        return f"{self.name} ({self.valueType.value})"

    def __init__(self, name):
        self.name = name
        self.tags = []
        self.multiple = False
        self.valueType = None

    def set_annotation_field(self):
        self.tags.append(ReservedTags.ANNOTATION.value)
        return self

    def set_type(self, fieldDataType: DagshubDataType):
        self.valueType = fieldDataType.get_corressponding_field_type()
        return self

    def update(self):
        # datasource.source.client.update_metadata_fields(datasource, [FieldMetadataUpdate(name="test", tags=[])])
        # To be implemented
        return self

    def is_annotation(self):
        return ReservedTags.ANNOTATION.value in self.tags


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


class ReservedTags(enum.Enum):
    ANNOTATION = "annotation"
