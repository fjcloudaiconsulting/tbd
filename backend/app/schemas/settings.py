import datetime
from typing import Literal

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


# TBD-197 — the planning-tool allow-list. A closed Literal, deliberately:
# typed as ``str`` this path parameter would hand an org admin an opt-out for
# ``reports`` / ``plans`` / ``custom_dashboard``, which are platform rollout
# flags rather than tenant preferences. Anything outside the list 422s at the
# framework boundary, before the handler body runs.
#
# ⚠ PR 1 ships ``"budgets"`` ALONE, not the pair. PR 1 gates zero Forecast
# routes, so accepting ``"forecast"`` here would let an admin write
# ``orgpref.forecast="off"``: the nav entry vanishes while every Forecast route
# stays wide open — and because the card renders only the Budgets switch there
# is no control left to turn it back on, so the org cannot recover from the UI.
# An allow-list must never run ahead of the gates it is an allow-list for.
# PR 2 widens this back to ``Literal["forecast", "budgets"]`` in the same commit
# that lands the Forecast route gates (spec §9).
PlanningTool = Literal["budgets"]


class PlanningToolToggle(BaseModel):
    enabled: bool


class PlanningToolResponse(BaseModel):
    feature: PlanningTool
    # The RE-RESOLVED effective value, which may disagree with the request: a
    # global "off" still wins over an org enable. That disagreement is exactly
    # what the settings UI reads to render "set by your administrator".
    enabled: bool
