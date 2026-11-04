"""Goals inside a plan (``plan_goals``, TBD-601).

``tier`` and ``kind`` are VARCHAR columns (migration 100): requests are
checked against the app's enums, responses carry the stored string.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.scenario import PlanGoalKind, PlanGoalTier
from app.schemas.common import Money
from app.schemas.scenario import NAME_MAX_LENGTH, NAME_MIN_LENGTH

# Columns that may be cleared with an explicit null on PATCH.
_NULLABLE = {
    "target_amount", "target_date", "monthly_amount", "category_id", "account_id",
    "start_balance", "funding_recurring_id",
}


class PlanGoalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=NAME_MIN_LENGTH, max_length=NAME_MAX_LENGTH)
    tier: PlanGoalTier
    kind: PlanGoalKind
    target_amount: Optional[Money] = Field(default=None, ge=0)
    target_date: Optional[date] = None
    monthly_amount: Optional[Money] = Field(default=None, ge=0)
    enabled: bool = True
    sort_order: int = Field(default=0, ge=0, le=10_000)
    category_id: Optional[int] = None
    account_id: Optional[int] = None
    start_balance: Optional[Money] = None
    funding_recurring_id: Optional[int] = None


class PlanGoalUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=NAME_MIN_LENGTH, max_length=NAME_MAX_LENGTH)
    tier: Optional[PlanGoalTier] = None
    kind: Optional[PlanGoalKind] = None
    target_amount: Optional[Money] = Field(default=None, ge=0)
    target_date: Optional[date] = None
    monthly_amount: Optional[Money] = Field(default=None, ge=0)
    enabled: Optional[bool] = None
    sort_order: Optional[int] = Field(default=None, ge=0, le=10_000)
    category_id: Optional[int] = None
    account_id: Optional[int] = None
    start_balance: Optional[Money] = None
    funding_recurring_id: Optional[int] = None

    @model_validator(mode="after")
    def _no_null_on_required(self):
        for name in self.model_fields_set - _NULLABLE:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self

    def changes(self) -> dict[str, Any]:
        """The fields the request sent, explicit nulls included."""
        return {k: getattr(self, k) for k in self.model_fields_set}


class PlanGoalResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    scenario_id: int
    name: str
    tier: str
    kind: str
    target_amount: Optional[Decimal] = None
    target_date: Optional[date] = None
    monthly_amount: Optional[Decimal] = None
    enabled: bool
    sort_order: int
    category_id: Optional[int] = None
    account_id: Optional[int] = None
    start_balance: Optional[Decimal] = None
    funding_recurring_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime
