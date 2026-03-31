from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class AccountTypeCreate(BaseModel):
    name: str


class AccountTypeUpdate(BaseModel):
    name: str


class AccountTypeResponse(BaseModel):
    id: int
    name: str
    slug: Optional[str] = None
    is_system: bool = False
    account_count: int = 0

    model_config = {"from_attributes": True}


class AccountCreate(BaseModel):
    name: str
    account_type_id: int
    balance: Decimal = Decimal("0.00")
    currency: str = "EUR"
    close_day: Optional[int] = Field(default=None, ge=1, le=28)


class AccountUpdate(BaseModel):
    name: Optional[str] = None
    account_type_id: Optional[int] = None
    is_active: Optional[bool] = None
    close_day: Optional[int] = Field(default=None, ge=1, le=28)
    is_default: Optional[bool] = None


class AccountResponse(BaseModel):
    id: int
    name: str
    account_type_id: int
    account_type_name: str = ""
    account_type_slug: Optional[str] = None
    balance: Decimal
    currency: str
    is_active: bool
    close_day: Optional[int] = None
    is_default: bool = False

    model_config = {"from_attributes": True}


class ReconcileResponse(BaseModel):
    account_id: int
    stored_balance: Decimal
    computed_balance: Decimal
    is_consistent: bool
