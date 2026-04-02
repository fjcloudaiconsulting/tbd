import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field


class RecurringCreate(BaseModel):
    account_id: int
    category_id: int
    description: str
    amount: Decimal = Field(gt=0)
    type: Literal["income", "expense"]
    frequency: Literal["weekly", "biweekly", "monthly", "quarterly", "yearly"]
    next_due_date: datetime.date
    auto_settle: bool = False


class RecurringUpdate(BaseModel):
    account_id: Optional[int] = None
    category_id: Optional[int] = None
    description: Optional[str] = None
    amount: Optional[Decimal] = Field(default=None, gt=0)
    type: Optional[Literal["income", "expense"]] = None
    frequency: Optional[Literal["weekly", "biweekly", "monthly", "quarterly", "yearly"]] = None
    next_due_date: Optional[datetime.date] = None
    auto_settle: Optional[bool] = None
    is_active: Optional[bool] = None


class RecurringResponse(BaseModel):
    id: int
    account_id: int
    account_name: str = ""
    category_id: int
    category_name: str = ""
    description: str
    amount: Decimal
    type: Literal["income", "expense"]
    frequency: str
    next_due_date: datetime.date
    auto_settle: bool
    is_active: bool

    model_config = {"from_attributes": True}
