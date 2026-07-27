import datetime

from pydantic import BaseModel, Field, model_validator


class OrgSettingUpdate(BaseModel):
    key: str
    value: str


class BillingCycleUpdate(BaseModel):
    billing_cycle_day: int = Field(ge=1, le=28)


class BillingPeriodCreate(BaseModel):
    """Request body for ``POST /api/v1/settings/billing-period``.

    Was an unvalidated query parameter (``start_date: datetime.date = None``)
    in front of a NOT NULL column: FastAPI marked it optional because a
    default was supplied and Pydantic v2 does not validate defaults, so
    ``None`` reached the handler and died at commit as an unhandled 500.

    Ordering is enforced here as a ``model_validator`` (yielding a
    framework-shaped 422) rather than in the router, matching
    ``schemas/announcement.py`` / ``schemas/org_ai_caps.py``. One endpoint
    returning both 400 and 422 for shape errors would be incoherent.
    """

    start_date: datetime.date
    end_date: datetime.date | None = None

    @model_validator(mode="after")
    def _check_order(self):
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date cannot be before start_date")
        return self


class OrgSettingResponse(BaseModel):
    key: str
    value: str

    model_config = {"from_attributes": True}


class ManualBalanceAdjustmentToggle(BaseModel):
    enabled: bool


class ManualBalanceAdjustmentResponse(BaseModel):
    enabled: bool
