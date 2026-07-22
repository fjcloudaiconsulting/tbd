"""Per-cycle credit-card payment amounts (Credit Card Model V1, Slice 2).

A dedicated child table for the amount the user plans to pay for a
given CC billing cycle (``specs/2026-07-22-cc-model-v1-design.md``
§ "Migration 074"). NOT an extension of the never-shipped
``cc_cycle_overrides``; depends only on the shipped
``cc_cycle_service`` resolver.

- Anchor = the cycle's CLOSE month
  (``period_end_inclusive.year`` / ``.month``). A Jan-25 close paid
  Feb-1 stores under ``(account, 2026, 1)``.
- No ``org_id`` column — org isolation is enforced at the router by
  loading the parent account under ``current_user.org_id``.
- ``ON DELETE CASCADE`` because a payment row is meaningless without
  its account; org-wipe/reset and the leave-CC path delete these
  rows explicitly anyway (defense in depth + accurate counts).
- ``amount`` is ``NOT NULL`` (no CHECK needed — a stored row always
  carries a real amount; "unset" is the absence of a row).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CcCyclePayment(Base):
    __tablename__ = "cc_cycle_payments"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "period_anchor_year",
            "period_anchor_month",
            name="uq_cc_cycle_payments_account_period",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    period_anchor_year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    period_anchor_month: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
