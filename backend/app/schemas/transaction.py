import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class TransactionCreate(BaseModel):
    account_id: int
    category_id: int
    description: str = Field(max_length=255)
    amount: Decimal = Field(gt=0)
    type: Literal["income", "expense"]
    status: Literal["settled", "pending"] = "settled"
    date: datetime.date

    @field_validator("description")
    @classmethod
    def description_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Description is required")
        return v.strip()


class TransferCreate(BaseModel):
    from_account_id: int
    to_account_id: int
    category_id: Optional[int] = None
    description: str = Field(default="", max_length=255)
    amount: Decimal = Field(gt=0)
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

    @field_validator("description")
    @classmethod
    def description_not_empty(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("Description is required")
        return v.strip() if v is not None else v


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
    linked_transaction_id: Optional[int] = None
    recurring_id: Optional[int] = None
    date: datetime.date
    settled_date: datetime.date | None = None
    is_imported: bool = False

    model_config = {"from_attributes": True}
