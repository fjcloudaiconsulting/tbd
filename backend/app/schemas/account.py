from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field

from app.models.account import PaymentStrategy


# Mirrors the Numeric(12, 2) DB constraint on accounts.opening_balance.
# A schema-level range produces a clean 422 instead of a DB-overflow 500.
_OPENING_BALANCE_CAP_HI = Decimal("9999999999.99")
_OPENING_BALANCE_CAP_LO = Decimal("-9999999999.99")

# Loan Account Type V1. Parse-time belt for the loan columns (the
# "required-on-loan" coupling still lives in loan_service since the schema
# can't see the target slug). interest_rate_apr mirrors Numeric(5, 2).
_LOAN_APR_CAP_HI = Decimal("999.99")


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
    close_day: Optional[int] = Field(default=None, ge=1, le=31)
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
    # Payment Source Foundation: the account this liability's bill is paid
    # FROM. Optional; validated server-side (same-org, checking/savings/cash,
    # not self, active) in payment_source_service.
    payment_source_account_id: Optional[int] = None
    # Credit Card Model V1 (Slice 1). CC-only; validated server-side in
    # credit_card_service. NULL on non-CC accounts. payment_strategy is a
    # native enum (NULL = resolver default). fixed_payment_amount is
    # required iff payment_strategy == fixed_amount.
    credit_limit: Optional[Decimal] = Field(
        default=None, max_digits=12, decimal_places=2
    )
    apr: Optional[Decimal] = Field(default=None, max_digits=12, decimal_places=2)
    payment_strategy: Optional[PaymentStrategy] = None
    fixed_payment_amount: Optional[Decimal] = Field(
        default=None, max_digits=12, decimal_places=2
    )
    # Loan Account Type V1 (Slice 1). Loan-only; validated server-side in
    # loan_service (required-on-loan / forbidden-off-loan). principal_amount is
    # the ORIGINAL contractual principal; the live owed amount rides
    # opening_balance/balance and is independent (mid-life import allowed).
    principal_amount: Optional[Decimal] = Field(
        default=None, gt=0, max_digits=12, decimal_places=2
    )
    interest_rate_apr: Optional[Decimal] = Field(
        default=None, ge=0, le=_LOAN_APR_CAP_HI, max_digits=5, decimal_places=2
    )
    term_months: Optional[int] = Field(default=None, ge=1, le=480)
    origination_date: Optional[date] = None
    first_payment_date: Optional[date] = None


class AccountUpdate(BaseModel):
    name: Optional[str] = None
    account_type_id: Optional[int] = None
    is_active: Optional[bool] = None
    close_day: Optional[int] = Field(default=None, ge=1, le=31)
    payment_day: Optional[int] = Field(default=None, ge=1, le=31)
    payment_day_relative_month: Optional[int] = Field(default=None, ge=0, le=12)
    is_default: Optional[bool] = None
    # Payment Source Foundation. Uses the model_fields_set idiom in the
    # router: omit to preserve, send explicit null to clear the pointer.
    payment_source_account_id: Optional[int] = None
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
    # Credit Card Model V1 (Slice 1). See AccountCreate for field semantics.
    credit_limit: Optional[Decimal] = Field(
        default=None, max_digits=12, decimal_places=2
    )
    apr: Optional[Decimal] = Field(default=None, max_digits=12, decimal_places=2)
    payment_strategy: Optional[PaymentStrategy] = None
    fixed_payment_amount: Optional[Decimal] = Field(
        default=None, max_digits=12, decimal_places=2
    )
    # Loan Account Type V1 (Slice 1). See AccountCreate for field semantics.
    # model_fields_set idiom in the router: omit to preserve, send explicit
    # null to clear (loan columns are cleared server-side on leaving the type).
    principal_amount: Optional[Decimal] = Field(
        default=None, gt=0, max_digits=12, decimal_places=2
    )
    interest_rate_apr: Optional[Decimal] = Field(
        default=None, ge=0, le=_LOAN_APR_CAP_HI, max_digits=5, decimal_places=2
    )
    term_months: Optional[int] = Field(default=None, ge=1, le=480)
    origination_date: Optional[date] = None
    first_payment_date: Optional[date] = None


class LoanMetrics(BaseModel):
    """Computed loan metrics (no storage), projected into AccountResponse.loan
    for loan accounts with all five loan fields set. See loan_service."""

    expected_monthly_payment: Decimal
    maturation_date: date
    total_interest: Decimal
    projected_payoff_date: Optional[date] = None
    projected_payoff_months: Optional[int] = None
    status: str  # "on_track" | "paid_off" | "interest_only"


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
    payment_source_account_id: Optional[int] = None
    credit_limit: Optional[Decimal] = None
    apr: Optional[Decimal] = None
    payment_strategy: Optional[PaymentStrategy] = None
    fixed_payment_amount: Optional[Decimal] = None
    principal_amount: Optional[Decimal] = None
    interest_rate_apr: Optional[Decimal] = None
    term_months: Optional[int] = None
    origination_date: Optional[date] = None
    first_payment_date: Optional[date] = None
    # Computed (no storage); populated only for fully-specified loan accounts.
    loan: Optional[LoanMetrics] = None

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
