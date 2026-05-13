"""SQLAlchemy model for ``import_batches`` (L3.2 Wave 2B).

An ``ImportBatch`` is the header row for a single CSV / OFX upload. It
groups the imported transactions for the post-confirm reconciliation
inbox UX.

Contract: ``specs/2026-05-12-l3-2-import-contracts.md`` §3.2 / §3.2.1 /
§3.2.2. The row is created in ``reconciliation_service.create_import_batch``
at confirm time, and auto-closed by ``close_batch_if_complete`` when
every contained transaction lands in a terminal state.

Note: ``source_format`` is intentionally limited to CSV / OFX. Manual
batch entry (``POST /api/v1/transactions/batch``) is NOT a
reconciliation source and never creates an ``ImportBatch`` row.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ImportSourceFormat(str, enum.Enum):
    """Source format for an ``import_batches`` row.

    Mirrors ``app.schemas.import_reconciliation.ImportSourceFormat`` so
    the wire enum and the ORM enum agree by construction.
    """

    CSV = "csv"
    OFX = "ofx"


class ImportBatchStatus(str, enum.Enum):
    """Lifecycle status for an ``import_batches`` row.

    ``OPEN`` while any contained transaction is still ``PENDING_REVIEW``
    or ``UNMATCHED``. Auto-flips to ``CLOSED`` (with ``closed_at`` set)
    by ``reconciliation_service.close_batch_if_complete`` on the last
    transition that drives ``pending_count`` to zero.
    """

    OPEN = "open"
    CLOSED = "closed"


class ImportBatch(Base):
    """Header row for one import batch (CSV or OFX upload).

    Counter columns (``row_count``, ``accepted_count``, ``pending_count``)
    are denormalized on the row to spare the reconciliation UI from a
    GROUP BY across ``transactions`` on every paint. The service layer
    keeps them in sync as the batch is created and as rows are
    reconciled.
    """

    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id"), nullable=False
    )
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id"), nullable=False
    )
    source_format: Mapped[ImportSourceFormat] = mapped_column(
        Enum(
            ImportSourceFormat,
            values_callable=lambda x: [e.value for e in x],
            name="import_source_format",
        ),
        nullable=False,
    )
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    created_by_user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False
    )
    status: Mapped[ImportBatchStatus] = mapped_column(
        Enum(
            ImportBatchStatus,
            values_callable=lambda x: [e.value for e in x],
            name="import_batch_status",
        ),
        nullable=False,
        default=ImportBatchStatus.OPEN,
        server_default=ImportBatchStatus.OPEN.value,
    )
    row_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    accepted_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    pending_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
