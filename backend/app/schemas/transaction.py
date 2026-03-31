import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field


class TransactionCreate(BaseModel):
    account_id: int
    category_id: int
    description: str
    amount: Decimal = Field(gt=0)
    type: Literal["income", "expense"]
    status: Literal["settled", "pending"] = "settled"
    date: datetime.date


class TransactionUpdate(BaseModel):
    account_id: Optional[int] = None
    category_id: Optional[int] = None
    description: Optional[str] = None
    amount: Optional[Decimal] = Field(default=None, gt=0)
    type: Optional[Literal["income", "expense"]] = None
    status: Optional[Literal["settled", "pending"]] = None
    date: Optional[datetime.date] = None


class TransactionResponse(BaseModel):
    id: int
    account_id: int
    account_name: str = ""
    category_id: int
    category_name: str = ""
    description: str
    amount: Decimal
    type: Literal["income", "expense"]
    status: Literal["settled", "pending"]
    date: datetime.date

    model_config = {"from_attributes": True}
