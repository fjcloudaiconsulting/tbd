"""The ``Idempotency-Key`` of one manual batch create (TBD-784).

One row per keyed ``POST /api/v1/transactions/batch``. It is inserted before
the rows and committed with them, so a repeat with the same key either waits
on the unique index (the first request is still running) or finds the stored
``response`` and returns it without writing.

* The key is scoped to (org, user): ``UNIQUE (idempotency_key, org_id,
  user_id)``, key first so the index cannot stand in for the FK indexes.
* ``request_hash`` fingerprints the body; the same key with another body is a 409.
* A key expires ``KEY_TTL_HOURS`` after ``created_at``: a later request with
  it writes as new, and ``jobs/batch_key_purge`` deletes the row. Timestamps
  are naive UTC, written app-side so SQLite and MySQL compare alike.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TransactionBatchKey(Base):
    __tablename__ = "transaction_batch_keys"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key", "org_id", "user_id",
            name="uq_transaction_batch_keys_key",
        ),
        Index("ix_transaction_batch_keys_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # NULL only inside the claiming transaction; committed rows always carry it.
    response: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
