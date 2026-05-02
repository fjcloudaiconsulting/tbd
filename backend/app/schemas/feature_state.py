"""Composite feature-state response for L4.3 admin org drill-down."""
from pydantic import BaseModel, ConfigDict

from app.schemas.feature_override import OrgFeatureOverrideResponse


class PlanSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str


class FeatureStateRow(BaseModel):
    key: str
    plan_default: bool
    effective: bool
    override: OrgFeatureOverrideResponse | None


class FeatureStateResponse(BaseModel):
    plan: PlanSummary | None
    features: list[FeatureStateRow]
