import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class BudgetCreate(BaseModel):
    category_id: int
    amount: Decimal = Field(gt=0)


class BudgetUpdate(BaseModel):
    amount: Optional[Decimal] = Field(default=None, gt=0)


class BudgetRebalanceItem(BaseModel):
    budget_id: int
    expected_amount: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    amount: Decimal = Field(ge=0, max_digits=12, decimal_places=2)


class BudgetRebalanceRequest(BaseModel):
    items: list[BudgetRebalanceItem] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def _no_duplicate_budget_ids(self) -> "BudgetRebalanceRequest":
        ids = [item.budget_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate budget_id in items")
        return self


class CopyBudgetsRequest(BaseModel):
    source_period_start: datetime.date
    target_period_start: Optional[datetime.date] = None


class BudgetResponse(BaseModel):
    id: int
    category_id: int
    category_name: str = ""
    amount: Decimal
    spent: Decimal = Decimal("0.00")
    remaining: Decimal = Decimal("0.00")
    percent_used: float = 0.0
    over_budget: bool = False
    period_start: datetime.date
    period_end: Optional[datetime.date] = None

    model_config = {"from_attributes": True}
