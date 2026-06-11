from pydantic import BaseModel


class SourceDimensionOut(BaseModel):
    key: str
    label: str
    kind: str


class SourceMeasureOut(BaseModel):
    key: str
    label: str
    agg: str
    field: str
    format: str


class SourceCatalogEntry(BaseModel):
    key: str
    label: str
    dimensions: list[SourceDimensionOut]
    measures: list[SourceMeasureOut]
