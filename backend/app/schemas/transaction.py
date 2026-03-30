import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class TransactionCreate(BaseModel):
    account_id: int
    category_id: int
    description: str
    amount: Decimal = Field(gt=0)
    type: str  # "income" or "expense"
    date: datetime.date


class TransactionUpdate(BaseModel):
    account_id: Optional[int] = None
    category_id: Optional[int] = None
    description: Optional[str] = None
    amount: Optional[Decimal] = Field(default=None, gt=0)
    type: Optional[str] = None
    date: Optional[datetime.date] = None


class TransactionResponse(BaseModel):
    id: int
    account_id: int
    account_name: str = ""
    category_id: int
    category_name: str = ""
    description: str
    amount: Decimal
    type: str
    date: datetime.date

    model_config = {"from_attributes": True}
