import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Frequency(str, enum.Enum):
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    YEARLY = "yearly"


class RecurringTransaction(Base):
    __tablename__ = "recurring_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id"), nullable=False
    )
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id"), nullable=False
    )
    # TBD-600: the destination of a ``transfer`` series, NULL for income and
    # expense. A transfer occurrence is a linked pair written through
    # ``transaction_service._link_pair``: an EXPENSE leg on ``account_id`` and
    # an INCOME leg here, both carrying ``recurring_id``. ``category_id`` is
    # then the pair's shared category and must be CategoryType.BOTH.
    to_account_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("accounts.id"), nullable=True
    )
    category_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("categories.id"), nullable=False
    )
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    type: Mapped[str] = mapped_column(
        Enum("income", "expense", "transfer", name="recurringtxtype"), nullable=False
    )
    frequency: Mapped[Frequency] = mapped_column(
        Enum(Frequency, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    next_due_date: Mapped[date] = mapped_column(Date, nullable=False)
    auto_settle: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # ── Instalment series (TBD-275) ───────────────────────────────────────
    # ``occurrence_count`` is the user's declared INTENT: how many occurrences
    # this series delivers in total. NULL means open-ended, which is what every
    # template created before this column existed is (no backfill, and none is
    # possible -- an open-ended template has no count to recover).
    #
    # Intent, not a constant: it is immutable only AS THE SERIES RUNS.
    # ``update_recurring`` writes it, in both directions -- a downward edit
    # below ``occurrences_elapsed`` is honoured and simply stops the series,
    # and an upward edit on a FINISHED series un-exhausts it and therefore
    # re-anchors the frontier exactly as a resume does. Nothing else may
    # change it.
    #
    # ``occurrences_elapsed`` is the series' PROGRESS, and it is STORED, never
    # counted. It is deliberately NOT named ``occurrences_generated``: a name
    # with "generated" in it invites verification by
    # ``COUNT(*) WHERE recurring_id = ...``, and that count is provably wrong.
    # ``stop_recurring`` NULLs ``recurring_id`` on every surviving row, the user
    # can delete a materialised row at any time, and the catch-up loop's
    # ``exists`` branch advances the frontier without creating a row at all.
    # Each of those makes the row count DROP or DIVERGE while the series has in
    # fact delivered the occurrence. Only a stored counter survives them.
    #
    # Exhaustion is DERIVED (``recurring_filters.remaining_occurrences``), never
    # written: an exhausted series keeps ``is_active = True`` and keeps its row.
    # See ``recurring_filters`` for why.
    occurrence_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    occurrences_elapsed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # The latest occurrence the frontier has consumed (TBD-509). A moved
    # ``next_due_date`` must land after it, so a rewind never re-covers a spent
    # occurrence. Rows cannot say this: stop NULLs their ``recurring_id`` and a
    # user may delete them, so it is stored, by ``_advance_frontier``. NULL on a
    # template that has spent nothing, and on those that spent before it existed.
    last_spent_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    account: Mapped["Account"] = relationship(foreign_keys=[account_id])
    to_account: Mapped[Optional["Account"]] = relationship(foreign_keys=[to_account_id])
    category: Mapped["Category"] = relationship()
