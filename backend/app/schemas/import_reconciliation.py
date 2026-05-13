"""Pydantic schemas for the post-import reconciliation flow (Wave 1 contract).

Frozen per spec at
``~/.claude/projects/-Users-fjorge-src-pfv/specs/2026-05-12-l3-2-import-contracts.md``.

Reconciliation is an optional post-confirm "inbox" UX. The DEFAULT state
for CSV- and OFX-confirm committed rows is ``ACCEPTED`` (Decision 3,
2026-05-12): this preserves the current CSV-import UX where confirm
commits transactions as final. The Reconciliation UI may later opt
specific formats into landing as ``PENDING_REVIEW`` (e.g. flag OFX as
"requires review" while keeping CSV at "accepted") on a per-format
basis; the contract supports either default but the locked baseline is
``ACCEPTED``.

Wave 2 Reconciliation UI team owns:
  - The migration that adds ``transactions.reconciliation_state``
    (NOT NULL, DEFAULT 'accepted'), ``transactions.import_batch_id``
    (nullable FK), and the new ``import_batches`` table.
  - BACKFILL: existing imported rows (``is_imported=True``) must be
    backfilled to ``reconciliation_state='accepted'`` so historical
    imports do not retroactively land in the review inbox.
  - The state-transition service and the inbox UI.

This file freezes the wire shape and the state-transition rules; it
does not implement them.
"""

from __future__ import annotations

import datetime
import enum
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ── State enum (server-authoritative) ──


class ReconciliationState(str, enum.Enum):
    """Reconciliation state of an imported transaction.

    Non-imported transactions are always ``ACCEPTED`` (the default for
    new rows on accounts).

    Allowed transitions (server enforces):

        PENDING_REVIEW → MATCHED | EDITED | SKIPPED | ACCEPTED | REJECTED
        MATCHED        → ACCEPTED                (implicit on close)
        EDITED         → ACCEPTED                (implicit on close)
        UNMATCHED      → MATCHED | EDITED | SKIPPED | ACCEPTED | REJECTED
        ACCEPTED       → PENDING_REVIEW          (reopen — rare)
        REJECTED       → (terminal)
        SKIPPED        → (terminal except via admin reopen, out of scope)

    Server stores values as the lowercase string. SQLAlchemy enum should
    declare ``values_callable=lambda x: [e.value for e in x]`` per the
    project convention.
    """

    PENDING_REVIEW = "pending_review"
    MATCHED = "matched"
    UNMATCHED = "unmatched"
    SKIPPED = "skipped"
    EDITED = "edited"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


# ── Source-format enum for import_batches ──


class ImportSourceFormat(str, enum.Enum):
    """Origin format for an ``import_batches`` row.

    Used for telemetry and to drive format-specific UX in the
    Reconciliation UI (e.g., show ``fitid`` column for OFX imports).

    NOTE: Manual batch entry (``POST /api/v1/transactions/batch``) is NOT
    a reconciliation source. Manual-batch rows are not flagged
    ``is_imported=True`` and they ship in Wave 2A before the
    ``import_batches`` table exists. They never appear in this enum.
    """

    CSV = "csv"
    OFX = "ofx"


# ── Import-batch header (response shape) ──


class ImportBatchHeader(BaseModel):
    """Header row for an import batch.

    Returned by ``GET /api/v1/import/{import_id}`` (Wave 2). Provides the
    Reconciliation UI with metadata to render the batch summary card.

    Fields:
        id: Batch primary key.
        account_id: Account the batch was imported into.
        source_format: Origin format (CSV or OFX). Manual batch entry is
            not a reconciliation source and never appears here.
        file_name: User-provided file name from the upload.
        created_at: When the batch was created.
        created_by_user_id: User who created the batch.
        status: ``open`` while any row is still ``PENDING_REVIEW``,
            ``closed`` once all rows are terminal.
        total_rows: Total transactions in this batch.
        pending_count: Rows still in ``PENDING_REVIEW`` or ``UNMATCHED``.
    """

    id: int
    account_id: int
    source_format: ImportSourceFormat
    file_name: str
    created_at: datetime.datetime
    created_by_user_id: int
    status: Literal["open", "closed"]
    total_rows: int = Field(ge=0)
    pending_count: int = Field(ge=0)

    model_config = ConfigDict(extra="forbid")


# ── Transition request shapes ──


class ReconciliationEdits(BaseModel):
    """Optional edits applied when transitioning a row to ``EDITED``.

    All fields are optional; only provided fields are updated. Server
    enforces the same validation as the standard transaction-update
    endpoint.

    Fields:
        description: New description.
        amount: New amount (positive Decimal).
        date: New date.
        category_id: New category.
    """

    description: str | None = Field(default=None, max_length=255)
    amount: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    date: datetime.date | None = None
    category_id: int | None = Field(default=None, gt=0)

    model_config = ConfigDict(extra="forbid")


class ReconciliationTransition(BaseModel):
    """A single transition request inside a reconcile batch.

    Fields:
        transaction_id: The transaction to transition. Must belong to the
            batch referenced in the URL path (``import_id``); server
            returns 422 otherwise.
        to_state: Target state. Server validates the (from, to) transition
            against the allowed-transitions table.
        edits: Required iff ``to_state == EDITED``. Forbidden otherwise.
        match_with_transaction_id: Required iff ``to_state == MATCHED``.
            The existing transaction this imported row links to.
            Forbidden otherwise.
    """

    transaction_id: int = Field(gt=0)
    to_state: ReconciliationState
    edits: ReconciliationEdits | None = None
    match_with_transaction_id: int | None = Field(default=None, gt=0)

    model_config = ConfigDict(extra="forbid")


class ReconcileBatchRequest(BaseModel):
    """Request body for ``POST /api/v1/import/{import_id}/reconcile``.

    All transitions in a single request commit atomically (one savepoint).
    If any transition is invalid (bad ``to_state``, missing required
    edits / match target, transaction belongs to a different batch, etc.)
    the entire request is rejected with 422 and no state changes.

    Fields:
        transitions: Ordered list of state transitions. Server applies
            them in order inside a single transaction.
    """

    transitions: list[ReconciliationTransition] = Field(min_length=1, max_length=500)

    model_config = ConfigDict(extra="forbid")


# ── Response shapes ──


class ReconciliationError(BaseModel):
    """Error detail for a single transition that failed.

    Note: in practice, errors here mean the WHOLE request rolled back
    (transitions are atomic). The errors list exists to tell the user
    which row(s) tripped the validation so the frontend can highlight
    them. Empty when all transitions applied.

    Fields:
        transaction_id: The transaction that triggered the error.
        error: Human-readable error message.
    """

    transaction_id: int
    error: str

    model_config = ConfigDict(extra="forbid")


class ReconciliationRow(BaseModel):
    """Per-transaction snapshot for the reconciliation inbox UI.

    Returned by ``GET /api/v1/import/{import_id}`` (Wave 2B). Carries
    just enough state for the per-row controls (current state, link
    target, duplicate signal). The frontend fetches transaction details
    on demand for the Edit dialog.

    The ``duplicate_warning`` boolean is set when ``fitid`` matches an
    existing transaction in the org outside this batch -- the "Possible
    duplicate of ..." callout in the recon UI.
    """

    transaction_id: int
    date: datetime.date
    description: str
    amount: Decimal
    type: Literal["income", "expense"]
    reconciliation_state: ReconciliationState
    fitid: str | None = None
    linked_transaction_id: int | None = None
    duplicate_warning: bool = False
    duplicate_warning_target: int | None = None

    model_config = ConfigDict(extra="forbid")


class ImportBatchDetail(BaseModel):
    """Full payload for ``GET /api/v1/import/{import_id}``.

    Combines the batch header with the per-row reconciliation snapshot
    so the frontend can render the full inbox in a single fetch.
    """

    batch: ImportBatchHeader
    rows: list[ReconciliationRow] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class ReconcileBatchResponse(BaseModel):
    """Response body for the reconcile endpoint.

    Fields:
        import_id: The batch that was reconciled.
        transitioned: Transaction IDs whose state changed. Same length as
            request.transitions when no errors.
        errors: Per-row errors (empty when transitions applied).
        remaining_pending: Count of rows still in ``PENDING_REVIEW`` or
            ``UNMATCHED`` after this request. When zero, the batch is
            auto-closed (``status='closed'``).
        batch_status: New batch status after this request.
    """

    import_id: int
    transitioned: list[int] = Field(default_factory=list)
    errors: list[ReconciliationError] = Field(default_factory=list)
    remaining_pending: int = Field(ge=0)
    batch_status: Literal["open", "closed"]

    model_config = ConfigDict(extra="forbid")
