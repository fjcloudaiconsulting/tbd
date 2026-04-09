import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class BudgetCreate(BaseModel):
    category_id: int
    amount: Decimal = Field(gt=0)


class BudgetUpdate(BaseModel):
    amount: Optional[Decimal] = Field(default=None, gt=0)


class BudgetTransfer(BaseModel):
    from_budget_id: int
    to_category_id: int
    amount: Decimal = Field(gt=0)


class BudgetResponse(BaseModel):
    id: int
    category_id: int
    category_name: str = ""
    amount: Decimal
    spent: Decimal = Decimal("0.00")
    remaining: Decimal = Decimal("0.00")
    percent_used: float = 0.0
    period_start: datetime.date
    period_end: Optional[datetime.date] = None

    model_config = {"from_attributes": True}
