from decimal import Decimal

from pydantic import BaseModel


class AccountTypeCreate(BaseModel):
    name: str


class AccountTypeUpdate(BaseModel):
    name: str


class AccountTypeResponse(BaseModel):
    id: int
    name: str
    account_count: int = 0

    model_config = {"from_attributes": True}


class AccountCreate(BaseModel):
    name: str
    account_type_id: int
    balance: Decimal = Decimal("0.00")
    currency: str = "EUR"


class AccountUpdate(BaseModel):
    name: str | None = None
    account_type_id: int | None = None
    is_active: bool | None = None


class AccountResponse(BaseModel):
    id: int
    name: str
    account_type_id: int
    account_type_name: str = ""
    balance: Decimal
    currency: str
    is_active: bool

    model_config = {"from_attributes": True}


class ReconcileResponse(BaseModel):
    account_id: int
    stored_balance: Decimal
    computed_balance: Decimal
    is_consistent: bool
