"""Composite feature-state response for L4.3 admin org drill-down."""
from pydantic import BaseModel, ConfigDict

from app.schemas.feature_override import OrgFeatureOverrideResponse
from app.schemas.limit_override import OrgLimitOverrideResponse


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


class LimitValue(BaseModel):
    period: str
    limit: int | None


class LimitStateRow(BaseModel):
    meter: str
    module: str
    plan: LimitValue
    effective: LimitValue
    source: str  # "override" | "plan" | "default"
    override: OrgLimitOverrideResponse | None


class FeatureStateResponse(BaseModel):
    plan: PlanSummary | None
    features: list[FeatureStateRow]
    limits: list[LimitStateRow]
