import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


SYSTEM_ACCOUNT_TYPES = [
    {"slug": "checking", "name": "Checking"},
    {"slug": "savings", "name": "Savings"},
    {"slug": "credit_card", "name": "Credit Card"},
    {"slug": "investment", "name": "Investment"},
    {"slug": "cash", "name": "Cash"},
]


class PaymentStrategy(str, enum.Enum):
    FULL_BALANCE = "full_balance"
    MINIMUM_ONLY = "minimum_only"
    FIXED_AMOUNT = "fixed_amount"
    CUSTOM_PER_PERIOD = "custom_per_period"


class AccountType(Base):
    __tablename__ = "account_types"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    accounts: Mapped[list["Account"]] = relationship(back_populates="account_type")


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id"), nullable=False
    )
    account_type_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("account_types.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    balance: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    close_day: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    payment_day: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    payment_day_relative_month: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Payment Source Foundation (specs/payment-source-account-foundation.md).
    # The account a liability's bill is paid FROM (self-referential FK to
    # accounts.id). Nullable; set only on liability accounts (credit_card,
    # and loan once that type lands). Source must be a checking/savings/cash
    # account in the same org, active, and not the target itself — enforced in
    # payment_source_service, not at the schema level. ``ON DELETE SET NULL``
    # (migration 072) clears the pointer when the source account is deleted.
    payment_source_account_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    # Credit Card Model V1 (specs/2026-07-22-cc-model-v1-design.md).
    # Four CC-only columns, NULL-at-rest on non-CC rows (fat-account-row
    # idiom, mirroring close_day). credit_limit is optional + non-enforcing;
    # apr is percent metadata [0,100]; fixed_payment_amount is required iff
    # payment_strategy == fixed_amount. payment_strategy is a native MySQL
    # ENUM; NULL means "resolver default (full_balance)". Validation lives in
    # credit_card_service, not the schema level.
    credit_limit: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    apr: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    fixed_payment_amount: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    payment_strategy: Mapped[Optional[PaymentStrategy]] = mapped_column(
        SAEnum(
            PaymentStrategy,
            name="account_payment_strategy",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=True,
    )
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # User-stated opening balance for the account. Migration 041 sets
    # this to 0 for every existing account (canonical backfill, see
    # contract §4.4). New accounts may set it at create time; existing
    # accounts may edit it via PATCH /api/v1/accounts/{id}.
    opening_balance: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00"), server_default="0"
    )
    opening_balance_date: Mapped[date] = mapped_column(
        Date, nullable=False, server_default=func.current_date()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    account_type: Mapped["AccountType"] = relationship(back_populates="accounts")
