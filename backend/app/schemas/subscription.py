from decimal import Decimal

from pydantic import BaseModel, Field


class PlanResponse(BaseModel):
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
    ai_budget_enabled: bool
    ai_forecast_enabled: bool
    ai_smart_plan_enabled: bool

    model_config = {"from_attributes": True}


class PlanCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    slug: str = Field(min_length=1, max_length=50, pattern=r"^[a-z0-9-]+$")
    description: str = ""
    is_custom: bool = False
    sort_order: int = 0
    price_monthly: Decimal = Field(ge=0, default=0)
    price_yearly: Decimal = Field(ge=0, default=0)
    max_users: int | None = Field(default=None, ge=1)
    retention_days: int | None = Field(default=None, ge=1)
    ai_budget_enabled: bool = False
    ai_forecast_enabled: bool = False
    ai_smart_plan_enabled: bool = False


class PlanUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    is_custom: bool | None = None
    is_active: bool | None = None
    sort_order: int | None = None
    price_monthly: Decimal | None = Field(default=None, ge=0)
    price_yearly: Decimal | None = Field(default=None, ge=0)
    max_users: int | None = None
    retention_days: int | None = None
    ai_budget_enabled: bool | None = None
    ai_forecast_enabled: bool | None = None
    ai_smart_plan_enabled: bool | None = None


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
