"""Responses of the plan projection routes (TBD-601), read from
``plan_projection_service``'s results."""

from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict

# (date, balance) pairs, in order.
Series = list[tuple[date, Decimal]]


class _FromService(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class OverlayResponse(_FromService):
    plan_id: int
    goal_id: Optional[int]
    month: date
    on: date
    amount: Decimal
    goal_account_id: Optional[int]
    from_pool: bool


class OverFundingResponse(_FromService):
    plan_id: int
    goal_id: Optional[int]
    month: date
    excess: Decimal


class MonthReconciliationResponse(_FromService):
    month: date
    free_pool_net: Decimal
    surplus_ex_plans: Decimal
    allocated: Decimal
    under_over: Decimal
    annualised: Decimal


class GoalRequirementResponse(_FromService):
    goal_id: int
    monthly: Optional[Decimal]  # None: unreachable


class LifetimeResponse(_FromService):
    months: list[date]
    ages: list[Optional[int]]
    nominal: list[Decimal]
    real: list[Decimal]
    goal_accounts: dict[int, list[Decimal]]
    depletion_month: Optional[date]
    depletion_age: Optional[int]
    required: list[GoalRequirementResponse]


class PlanProjectionResponse(_FromService):
    currency: str
    actual: Series
    with_plans: Series
    goal_accounts: dict[int, Series]
    months: list[MonthReconciliationResponse]  # the reconciliation
    overlays: list[OverlayResponse]
    over_funded: list[OverFundingResponse]
    lifetime: Optional[LifetimeResponse] = None


class PlanComparisonResponse(BaseModel):
    plan_id: int
    projection: PlanProjectionResponse
