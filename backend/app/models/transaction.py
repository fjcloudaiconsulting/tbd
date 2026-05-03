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


class TransactionType(str, enum.Enum):
    INCOME = "income"
    EXPENSE = "expense"
    TRANSFER = "transfer"


class TransactionStatus(str, enum.Enum):
    SETTLED = "settled"
    PENDING = "pending"


class Transaction(Base):
    """Financial transaction.

    Transfers between own accounts are modeled as TWO paired rows (one
    EXPENSE on the source account, one INCOME on the destination), linked
    bidirectionally via ``linked_transaction_id`` (self-FK, ondelete=SET NULL).

    Transfer-pair invariants (enforced by the pairing service):
      1. Both rows belong to the same org.
      2. Linked bidirectionally.
      3. Opposite types (EXPENSE / INCOME).
      4. Equal absolute amounts.
      5. Different accounts.
      6. Same currency, evaluated as A.account.currency == B.account.currency.
      7. Neither row may already be linked to a third row before pairing.
      8. The reserved TransactionType.TRANSFER value is not used by the
         pairing model; legs are typed by direction.

    ``linked_transaction_id`` is **created** only by
    ``transaction_service._link_pair`` and **cleared** only by
    ``transaction_service.unpair_transactions``. Other code paths must not
    write this column directly.

    Reporting semantics: income/expense aggregates treat rows with
    ``linked_transaction_id IS NULL`` as reportable. Use
    ``app.services.transaction_filters.reportable_transaction_filter()``
    in queries and ``is_reportable_transaction(tx)`` / ``is_transfer_leg(tx)``
    in Python predicates.
    """

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id"), nullable=False
    )
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id"), nullable=False
    )
    category_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("categories.id"), nullable=False
    )
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    type: Mapped[TransactionType] = mapped_column(
        Enum(TransactionType, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    status: Mapped[TransactionStatus] = mapped_column(
        Enum(TransactionStatus, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=TransactionStatus.SETTLED,
    )
    linked_transaction_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True
    )
    recurring_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("recurring_transactions.id", ondelete="SET NULL"), nullable=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    settled_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    is_imported: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    account: Mapped["Account"] = relationship()
    category: Mapped["Category"] = relationship(back_populates="transactions")
    linked_transaction: Mapped[Optional["Transaction"]] = relationship(
        foreign_keys=[linked_transaction_id], remote_side="Transaction.id"
    )
