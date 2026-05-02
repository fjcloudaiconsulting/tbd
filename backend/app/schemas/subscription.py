from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, computed_field, field_validator

from app.auth.feature_catalog import PlanFeatures


class PlanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str
    description: str
    is_custom: bool
    is_active: bool
    sort_order: int
    price_monthly: Decimal
    price_yearly: Decimal
    max_users: int | None
    retention_days: int | None
    features: dict[str, bool]

    @field_validator("features", mode="before")
    @classmethod
    def _canonicalize_features(cls, v):
        # Defensive read-side canonicalization. If storage somehow
        # drifts (manual SQL, partial migration), the wire shape stays
        # canonical so consumers don't break.
        return PlanFeatures.model_validate(v or {}).model_dump(by_alias=True)

    # CLEANUP-029: remove the three computed fields below when migration
    # 029 ships. Frontend `Plan` type and `/settings/billing/page.tsx`
    # tier-descriptor logic also migrate to read `features` directly.
    @computed_field  # type: ignore[misc]
    @property
    def ai_budget_enabled(self) -> bool:
        return self.features.get("ai.budget", False)

    @computed_field  # type: ignore[misc]
    @property
    def ai_forecast_enabled(self) -> bool:
        return self.features.get("ai.forecast", False)

    @computed_field  # type: ignore[misc]
    @property
    def ai_smart_plan_enabled(self) -> bool:
        return self.features.get("ai.smart_plan", False)


class PlanCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    slug: str = Field(min_length=1, max_length=50, pattern=r"^[a-z0-9-]+$")
    description: str = ""
    is_custom: bool = False
    sort_order: int = 0
    price_monthly: Decimal = Field(ge=0, default=0)
    price_yearly: Decimal = Field(ge=0, default=0)
    max_users: int | None = Field(default=None, ge=1)
    retention_days: int | None = Field(default=None, ge=1)
    features: dict[str, StrictBool] = Field(default_factory=dict)


class PlanUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    is_custom: bool | None = None
    is_active: bool | None = None
    sort_order: int | None = None
    price_monthly: Decimal | None = Field(default=None, ge=0)
    price_yearly: Decimal | None = Field(default=None, ge=0)
    max_users: int | None = Field(default=None, ge=1)
    retention_days: int | None = Field(default=None, ge=1)
    features: dict[str, StrictBool] | None = None


class PlanDuplicateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    slug: str = Field(min_length=1, max_length=50, pattern=r"^[a-z0-9-]+$")


class SubscriptionResponse(BaseModel):
    id: int
    org_id: int
    plan: PlanResponse
    status: str
    billing_interval: str
    trial_start: str | None
    trial_end: str | None
    current_period_start: str | None
    current_period_end: str | None

    model_config = {"from_attributes": True}


class ChangePlanRequest(BaseModel):
    plan_slug: str = Field(min_length=1, max_length=50)
    billing_interval: str = Field(default="monthly", pattern=r"^(monthly|yearly)$")
