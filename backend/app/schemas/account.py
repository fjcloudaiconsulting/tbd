from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


# Mirrors the Numeric(12, 2) DB constraint on accounts.opening_balance.
# A schema-level range produces a clean 422 instead of a DB-overflow 500.
_OPENING_BALANCE_CAP_HI = Decimal("9999999999.99")
_OPENING_BALANCE_CAP_LO = Decimal("-9999999999.99")


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
    currency: str = "EUR"
    close_day: Optional[int] = Field(default=None, ge=1, le=28)
    payment_day: Optional[int] = Field(default=None, ge=1, le=31)
    payment_day_relative_month: Optional[int] = Field(default=None, ge=0, le=12)
    # Opening balance (L3.2 Wave 2A). User-stated starting amount and
    # the sole entry point for a non-zero starting balance: the live
    # ``Account.balance`` field is initialised from this value server-
    # side. L1.1 L4 pentest follow-up removed the previously accepted
    # free-form ``balance`` create input, which seeded ``Account.balance``
    # with no transaction backing and no audit row.
    opening_balance: Decimal = Field(
        default=Decimal("0.00"),
        ge=_OPENING_BALANCE_CAP_LO,
        le=_OPENING_BALANCE_CAP_HI,
        max_digits=12,
        decimal_places=2,
    )
    opening_balance_date: Optional[date] = None


class AccountUpdate(BaseModel):
    name: Optional[str] = None
    account_type_id: Optional[int] = None
    is_active: Optional[bool] = None
    close_day: Optional[int] = Field(default=None, ge=1, le=28)
    payment_day: Optional[int] = Field(default=None, ge=1, le=31)
    payment_day_relative_month: Optional[int] = Field(default=None, ge=0, le=12)
    is_default: Optional[bool] = None
    # Both opening fields are editable post-create. Audit-logged on
    # change (see ``accounts.update_account``).
    opening_balance: Optional[Decimal] = Field(
        default=None,
        ge=_OPENING_BALANCE_CAP_LO,
        le=_OPENING_BALANCE_CAP_HI,
        max_digits=12,
        decimal_places=2,
    )
    opening_balance_date: Optional[date] = None


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
    payment_day: Optional[int] = None
    payment_day_relative_month: Optional[int] = None
    is_default: bool = False
    opening_balance: Decimal = Decimal("0.00")
    opening_balance_date: Optional[date] = None

    model_config = {"from_attributes": True}


class ReconcileResponse(BaseModel):
    account_id: int
    stored_balance: Decimal
    computed_balance: Decimal
    is_consistent: bool


# ── Track E: manual balance adjustment ────────────────────────────────────


# Hard cap mirrors the Numeric(12, 2) column on transactions.amount. The
# Field constraint produces a clean 422 instead of a DB-overflow 500 when
# someone slams the endpoint with an absurd target.
_BALANCE_CAP_HI = Decimal("9999999999.99")
_BALANCE_CAP_LO = Decimal("-9999999999.99")


class BalanceAdjustmentRequest(BaseModel):
    target_balance: Decimal = Field(
        ge=_BALANCE_CAP_LO,
        le=_BALANCE_CAP_HI,
        max_digits=12,
        decimal_places=2,
    )
    reason: Optional[str] = Field(default=None, max_length=200)


class BalanceAdjustmentResponse(BaseModel):
    account_id: int
    old_balance: Decimal
    new_balance: Decimal
    delta: Decimal
    transaction_id: int
