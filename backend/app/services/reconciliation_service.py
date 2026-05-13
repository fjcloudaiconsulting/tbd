"""Reconciliation service (L3.2 Wave 2B).

Owns the state-machine for post-import reconciliation. The wire shapes
come from ``app.schemas.import_reconciliation`` (frozen in Wave 1, see
``specs/2026-05-12-l3-2-import-contracts.md`` §3). This module is the
authoritative place that:

* creates an ``ImportBatch`` header when CSV / OFX confirm commits a
  batch of transactions,
* validates state-machine transitions and applies them atomically,
* keeps the denormalized ``row_count`` / ``accepted_count`` /
  ``pending_count`` columns in sync as rows transition,
* auto-closes the batch when ``pending_count`` drops to zero.

Public entry points:

* ``create_import_batch(db, org_id, user_id, account_id, source_format,
  file_name, transaction_ids) -> ImportBatch``
* ``reconcile_request(db, org_id, batch_id, request) ->
  ReconcileBatchResponse`` -- top-level handler called from the router.
* ``close_batch_if_complete(db, batch) -> bool`` -- auto-close helper.

The transition table (§3.3 / §3.4) is encoded as ``ALLOWED_TRANSITIONS``
below. Disallowed transitions raise ``ConflictError`` so FastAPI's global
handler maps them to HTTP 409 with the source + target state in the
detail message.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.import_batch import (
    ImportBatch,
    ImportBatchStatus,
    ImportSourceFormat,
)
from app.models.transaction import Transaction
from app.schemas.import_reconciliation import (
    ImportBatchDetail,
    ImportBatchHeader,
    ReconcileBatchRequest,
    ReconcileBatchResponse,
    ReconciliationError,
    ReconciliationRow,
    ReconciliationState,
    ReconciliationTransition,
)
from app.services.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
)

logger = structlog.get_logger()


# Allowed state transitions (server-authoritative; spec §3.4 / §0.3).
# The keys are SOURCE states; the values list the ALLOWED targets.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    ReconciliationState.PENDING_REVIEW.value: frozenset(
        {
            ReconciliationState.MATCHED.value,
            ReconciliationState.EDITED.value,
            ReconciliationState.SKIPPED.value,
            ReconciliationState.ACCEPTED.value,
            ReconciliationState.REJECTED.value,
        }
    ),
    ReconciliationState.UNMATCHED.value: frozenset(
        {
            ReconciliationState.MATCHED.value,
            ReconciliationState.EDITED.value,
            ReconciliationState.SKIPPED.value,
            ReconciliationState.ACCEPTED.value,
            ReconciliationState.REJECTED.value,
        }
    ),
    ReconciliationState.MATCHED.value: frozenset(
        {ReconciliationState.ACCEPTED.value}
    ),
    ReconciliationState.EDITED.value: frozenset(
        {ReconciliationState.ACCEPTED.value}
    ),
    # ACCEPTED is normally terminal but supports an explicit "reopen"
    # path back to PENDING_REVIEW (spec §0.3, "rare; supports 'wait, I
    # made a mistake'").
    ReconciliationState.ACCEPTED.value: frozenset(
        {ReconciliationState.PENDING_REVIEW.value}
    ),
    # SKIPPED and REJECTED are terminal (§3.4 invariant 2).
    ReconciliationState.SKIPPED.value: frozenset(),
    ReconciliationState.REJECTED.value: frozenset(),
}

# States that count toward the batch's ``pending_count``. The batch is
# auto-closed once no row is in any of these.
PENDING_STATES: frozenset[str] = frozenset(
    {
        ReconciliationState.PENDING_REVIEW.value,
        ReconciliationState.UNMATCHED.value,
    }
)


# ── Batch creation (called from CSV / OFX confirm) ──────────────────────────


async def create_import_batch(
    db: AsyncSession,
    *,
    org_id: int,
    user_id: int,
    account_id: int,
    source_format: ImportSourceFormat | str,
    file_name: str,
    transaction_ids: Iterable[int],
) -> ImportBatch:
    """Persist a new ``import_batches`` row and link the imported
    transactions to it.

    Called from ``import_service.execute_import`` once the per-row commit
    loop is done. The caller passes the IDs of every transaction it just
    committed (plain creates + import-source paired legs). NULL
    ``import_batch_id`` rows -- like the synthetic partner leg of a
    create_transfer_pair -- stay NULL because they didn't come from the
    bank file.

    The function:

    1. Coerces ``source_format`` to the model enum.
    2. Inserts the ``ImportBatch`` header.
    3. Backfills ``transactions.import_batch_id`` for the provided IDs.
    4. Sets denormalized counters: ``row_count``, ``accepted_count``,
       ``pending_count``. CSV / OFX confirm rows land as ACCEPTED per
       spec §3.2.1, so ``accepted_count == row_count`` and
       ``pending_count == 0`` at creation time.

    Caller owns the outer transaction; this function flushes but does
    not commit. ``import_service.execute_import`` already runs
    ``await db.commit()`` after the per-row loop.
    """
    ids = [tid for tid in transaction_ids if tid is not None]
    if not ids:
        # No bank-sourced rows to attach -- e.g., an OFX upload where
        # every row was dropped as duplicate. Don't create an empty
        # batch; that would clutter the inbox with a "0 rows" entry.
        raise ValidationError(
            "create_import_batch called with empty transaction_ids"
        )

    if isinstance(source_format, str):
        try:
            source_format_enum = ImportSourceFormat(source_format)
        except ValueError as exc:
            raise ValidationError(
                f"unknown source_format {source_format!r}; "
                "expected one of 'csv' or 'ofx'"
            ) from exc
    else:
        source_format_enum = source_format

    batch = ImportBatch(
        org_id=org_id,
        account_id=account_id,
        source_format=source_format_enum,
        file_name=file_name,
        created_by_user_id=user_id,
        status=ImportBatchStatus.OPEN,
        row_count=len(ids),
        # Decision 3 (§3.2.1): committed rows land ACCEPTED, so the
        # batch opens fully accepted with zero pending. The inbox shows
        # it as "closed" immediately because the auto-close helper runs
        # at the end of each ``reconcile_request`` -- but the
        # create-side keeps the batch ``OPEN`` so a future per-format
        # opt-in (e.g. flipping OFX-confirm to land ``PENDING_REVIEW``)
        # works without a schema change.
        accepted_count=len(ids),
        pending_count=0,
    )
    db.add(batch)
    await db.flush()  # populate batch.id

    # Backfill the FK on the imported transactions. Single UPDATE keeps
    # the row count off the per-row hot path and avoids loading the
    # transaction objects again.
    await db.execute(
        update(Transaction)
        .where(
            Transaction.id.in_(ids),
            Transaction.org_id == org_id,
        )
        .values(import_batch_id=batch.id)
    )

    await logger.ainfo(
        "import.batch_created",
        org_id=org_id,
        batch_id=batch.id,
        source_format=source_format_enum.value,
        row_count=len(ids),
    )

    return batch


# ── Batch fetch helpers ──────────────────────────────────────────────────────


async def get_batch_for_update(
    db: AsyncSession, *, org_id: int, batch_id: int
) -> ImportBatch:
    """Lock an ``import_batches`` row for the duration of a reconcile
    request. Raises ``NotFoundError`` when the batch is missing or
    belongs to another org (org-scoped 404 hides existence)."""
    result = await db.execute(
        select(ImportBatch)
        .where(
            ImportBatch.id == batch_id,
            ImportBatch.org_id == org_id,
        )
        .with_for_update()
    )
    batch = result.scalar_one_or_none()
    if batch is None:
        raise NotFoundError("ImportBatch")
    return batch


# ── Batch detail fetch (for GET inbox endpoint) ─────────────────────────────


async def get_batch_detail(
    db: AsyncSession, *, org_id: int, batch_id: int
) -> ImportBatchDetail:
    """Return the batch header + per-row reconciliation snapshot.

    Computes ``duplicate_warning`` per row by looking up cross-batch
    FITID matches: any transaction in the same org with the same
    ``fitid`` but a different ID lights the warning. This is the
    "Possible duplicate of <descr> on <date>" callout in the recon UI.
    The warning is informational; the user can override with "Mark as
    different" by accepting the row anyway.
    """
    batch = await db.scalar(
        select(ImportBatch).where(
            ImportBatch.id == batch_id,
            ImportBatch.org_id == org_id,
        )
    )
    if batch is None:
        raise NotFoundError("ImportBatch")

    tx_result = await db.execute(
        select(Transaction)
        .where(
            Transaction.import_batch_id == batch_id,
            Transaction.org_id == org_id,
        )
        .order_by(Transaction.id)
    )
    transactions = list(tx_result.scalars().all())

    # Cross-batch FITID warnings: find every transaction in the org
    # whose ``fitid`` matches a non-NULL fitid on a row in this batch
    # AND is NOT in this batch. Single query keeps the per-row check
    # off the hot path.
    fitids_in_batch = [t.fitid for t in transactions if t.fitid]
    dup_warning_map: dict[str, int] = {}
    if fitids_in_batch:
        dup_result = await db.execute(
            select(Transaction.id, Transaction.fitid).where(
                Transaction.org_id == org_id,
                Transaction.fitid.in_(fitids_in_batch),
                Transaction.import_batch_id != batch_id,
            )
        )
        for row in dup_result.all():
            if row.fitid and row.fitid not in dup_warning_map:
                dup_warning_map[row.fitid] = row.id

    rows: list[ReconciliationRow] = []
    for tx in transactions:
        warning_target = (
            dup_warning_map.get(tx.fitid) if tx.fitid else None
        )
        rows.append(
            ReconciliationRow(
                transaction_id=tx.id,
                date=tx.date,
                description=tx.description,
                amount=tx.amount,
                type=("income" if tx.type.value == "income" else "expense"),
                reconciliation_state=ReconciliationState(
                    tx.reconciliation_state
                ),
                fitid=tx.fitid,
                linked_transaction_id=tx.linked_transaction_id,
                duplicate_warning=warning_target is not None,
                duplicate_warning_target=warning_target,
            )
        )

    header = ImportBatchHeader(
        id=batch.id,
        account_id=batch.account_id,
        source_format=batch.source_format,
        file_name=batch.file_name,
        created_at=batch.created_at,
        created_by_user_id=batch.created_by_user_id,
        status=("closed" if batch.status.value == "closed" else "open"),
        total_rows=batch.row_count,
        pending_count=batch.pending_count,
    )

    return ImportBatchDetail(batch=header, rows=rows)


# ── Transition validation ───────────────────────────────────────────────────


def _validate_payload_shape(transition: ReconciliationTransition) -> None:
    """Enforce per-target payload invariants:

    * ``EDITED`` REQUIRES ``edits``;
    * ``MATCHED`` REQUIRES ``match_with_transaction_id``;
    * every other target FORBIDS both.
    """
    target = transition.to_state.value
    if target == ReconciliationState.EDITED.value:
        if transition.edits is None:
            raise ValidationError(
                "EDITED transition requires 'edits'"
            )
        if transition.match_with_transaction_id is not None:
            raise ValidationError(
                "EDITED transition does not accept 'match_with_transaction_id'"
            )
    elif target == ReconciliationState.MATCHED.value:
        if transition.match_with_transaction_id is None:
            raise ValidationError(
                "MATCHED transition requires 'match_with_transaction_id'"
            )
        if transition.edits is not None:
            raise ValidationError(
                "MATCHED transition does not accept 'edits'"
            )
    else:
        if transition.edits is not None:
            raise ValidationError(
                f"{target.upper()} transition does not accept 'edits'"
            )
        if transition.match_with_transaction_id is not None:
            raise ValidationError(
                f"{target.upper()} transition does not accept "
                "'match_with_transaction_id'"
            )


def _validate_transition(
    *, source_state: str, target_state: str
) -> None:
    """Look up (source, target) in ``ALLOWED_TRANSITIONS``. Raise
    ``ConflictError`` (-> 409) for any disallowed transition; the message
    spells out both states so the frontend can render an actionable
    diagnostic."""
    allowed = ALLOWED_TRANSITIONS.get(source_state)
    if allowed is None:
        # source_state value isn't in the enum at all. Shouldn't reach
        # here in practice -- the DB column is a tight ENUM -- but the
        # guard pays for itself the day someone writes an unbounded
        # string by mistake.
        raise ConflictError(
            f"unknown source state {source_state!r}"
        )
    if target_state not in allowed:
        raise ConflictError(
            f"reconciliation transition not allowed: "
            f"{source_state} -> {target_state}"
        )


# ── Single-row reconciliation ───────────────────────────────────────────────


async def _apply_edits(
    db: AsyncSession,
    *,
    tx: Transaction,
    transition: ReconciliationTransition,
) -> None:
    """Apply ``ReconciliationEdits`` to a transaction in place.

    Only updates the fields the user actually changed (the schema lets
    every field be ``None`` to mean "leave it alone"). The standard
    transaction validation lives in ``transaction_service.update_transaction``
    and is intentionally NOT routed through here: the reconciliation
    edits surface is narrow (description / amount / date / category)
    and doesn't need the partner-locking dance the transfer-pair
    invariant requires. A future iteration can promote the call to
    the full update path; for now, direct attribute writes keep the
    state-machine flow simple and small.
    """
    edits = transition.edits
    if edits is None:
        return
    if edits.description is not None:
        tx.description = edits.description
    if edits.amount is not None:
        # Note: amount changes here do NOT replay account-balance
        # bookkeeping. The reconciliation inbox is for fixing typos on
        # rows that already landed; the balance has already absorbed the
        # original amount. The PR description tracks this caveat.
        tx.amount = edits.amount
    if edits.date is not None:
        tx.date = edits.date
    if edits.category_id is not None:
        tx.category_id = edits.category_id


async def _apply_match(
    db: AsyncSession,
    *,
    org_id: int,
    tx: Transaction,
    transition: ReconciliationTransition,
) -> None:
    """Validate the match target and link it on ``tx``.

    The match target must:

    * belong to the same org,
    * be a real (existing) transaction,
    * be different from ``tx`` itself.

    The match is recorded by writing the target ID onto
    ``Transaction.linked_transaction_id``. We deliberately do NOT call
    ``_link_pair`` here -- transfer-pair invariants (opposite types,
    matched amounts, different accounts) are stricter than the user-
    facing "this imported row is the same thing as that existing one"
    intent. The narrower link is enough for the reconciliation inbox to
    show the relationship and for downstream reports to filter on it.
    """
    match_id = transition.match_with_transaction_id
    if match_id is None:
        raise ValidationError(
            "MATCHED transition requires 'match_with_transaction_id'"
        )
    if match_id == tx.id:
        raise ValidationError(
            "MATCHED target must differ from the transaction itself"
        )
    target = await db.scalar(
        select(Transaction).where(
            Transaction.id == match_id,
            Transaction.org_id == org_id,
        )
    )
    if target is None:
        raise NotFoundError("Match target transaction")
    tx.linked_transaction_id = match_id


async def _reconcile_one(
    db: AsyncSession,
    *,
    org_id: int,
    batch: ImportBatch,
    transition: ReconciliationTransition,
) -> Transaction:
    """Apply ONE transition. Validates payload shape, batch membership,
    and the (source, target) edge in the state machine. Returns the
    mutated transaction.

    Side-effects:

    * sets ``transaction.reconciliation_state``,
    * optionally applies edits / match metadata,
    * updates batch counters in memory (caller flushes at the end).
    """
    _validate_payload_shape(transition)

    target_state = transition.to_state.value
    tx = await db.scalar(
        select(Transaction).where(
            Transaction.id == transition.transaction_id,
            Transaction.org_id == org_id,
        )
    )
    if tx is None:
        raise NotFoundError("Transaction")
    if tx.import_batch_id != batch.id:
        # Spec §3.4 invariant 4: transitions on a transaction that
        # doesn't belong to ``import_id`` -> 422 (ValidationError).
        raise ValidationError(
            f"transaction {tx.id} does not belong to batch {batch.id}"
        )

    source_state = tx.reconciliation_state
    if source_state == target_state:
        # No-op: idempotent self-transition. Skip counter updates so the
        # batch stays consistent.
        return tx

    _validate_transition(
        source_state=source_state, target_state=target_state
    )

    # Optional payload application (edits / match) BEFORE the state flip
    # so a payload validation error doesn't leave the row in a half-
    # updated state.
    if target_state == ReconciliationState.EDITED.value:
        await _apply_edits(db, tx=tx, transition=transition)
    elif target_state == ReconciliationState.MATCHED.value:
        await _apply_match(
            db, org_id=org_id, tx=tx, transition=transition
        )

    tx.reconciliation_state = target_state

    # Counter bookkeeping.
    source_was_pending = source_state in PENDING_STATES
    target_is_pending = target_state in PENDING_STATES
    if source_was_pending and not target_is_pending:
        batch.pending_count = max(0, batch.pending_count - 1)
    elif (not source_was_pending) and target_is_pending:
        batch.pending_count += 1

    source_was_accepted = (
        source_state == ReconciliationState.ACCEPTED.value
    )
    target_is_accepted = (
        target_state == ReconciliationState.ACCEPTED.value
    )
    if source_was_accepted and not target_is_accepted:
        batch.accepted_count = max(0, batch.accepted_count - 1)
    elif (not source_was_accepted) and target_is_accepted:
        batch.accepted_count += 1

    return tx


# ── Auto-close helper ────────────────────────────────────────────────────────


async def close_batch_if_complete(
    db: AsyncSession, *, batch: ImportBatch
) -> bool:
    """Flip ``batch.status`` to ``CLOSED`` and stamp ``closed_at`` when
    no row is in a pending state. Returns True iff the batch transitioned
    on this call.

    Note: ``pending_count`` on the batch is the source of truth, kept in
    sync by ``_reconcile_one``. We could re-query the underlying rows
    here, but the denormalized counter is the whole point of the
    column. A defensive SQL count is run only if the counter is zero --
    a belt-and-braces guard against future code paths that mutate
    ``reconciliation_state`` without going through ``_reconcile_one``.
    """
    if batch.status == ImportBatchStatus.CLOSED:
        return False
    if batch.pending_count > 0:
        return False

    # Belt-and-braces re-check against the underlying rows.
    pending = await db.scalar(
        select(func.count(Transaction.id)).where(
            Transaction.import_batch_id == batch.id,
            Transaction.reconciliation_state.in_(list(PENDING_STATES)),
        )
    )
    if pending and pending > 0:
        # Counter drift -- fix it but stay open.
        batch.pending_count = int(pending)
        await logger.awarning(
            "import.batch_pending_count_drift",
            batch_id=batch.id,
            counter_value=0,
            actual=int(pending),
        )
        return False

    batch.status = ImportBatchStatus.CLOSED
    batch.closed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await logger.ainfo(
        "import.batch_auto_closed",
        batch_id=batch.id,
        row_count=batch.row_count,
    )
    return True


# ── Top-level request handler ───────────────────────────────────────────────


async def reconcile_request(
    db: AsyncSession,
    *,
    org_id: int,
    batch_id: int,
    request: ReconcileBatchRequest,
) -> ReconcileBatchResponse:
    """Apply every transition in ``request`` atomically.

    Spec §3.4 invariant 5: "All transitions atomic per request (one
    savepoint, all-or-nothing)." We wrap the per-row loop in a single
    ``db.begin_nested()``. Any error -- domain or unexpected --
    bubbles, the savepoint rolls back, and the response is the
    appropriate 4xx.

    The endpoint shape is "apply many, return summary". Errors propagate
    as exceptions and are mapped by the global handlers (ConflictError
    -> 409, ValidationError -> 422, NotFoundError -> 404). The
    ``errors`` field on the response stays empty in the success path --
    its presence in the schema is for forward-compat with a future
    best-effort variant.
    """
    batch = await get_batch_for_update(
        db, org_id=org_id, batch_id=batch_id
    )

    transitioned: list[int] = []
    errors: list[ReconciliationError] = []

    async with db.begin_nested():
        for transition in request.transitions:
            try:
                tx = await _reconcile_one(
                    db,
                    org_id=org_id,
                    batch=batch,
                    transition=transition,
                )
                transitioned.append(tx.id)
            except (
                ConflictError,
                NotFoundError,
                ValidationError,
            ):
                # Re-raise to roll back the savepoint -- the request is
                # all-or-nothing per §3.4.5.
                raise

        await close_batch_if_complete(db, batch=batch)

    await db.commit()

    return ReconcileBatchResponse(
        import_id=batch.id,
        transitioned=transitioned,
        errors=errors,
        remaining_pending=batch.pending_count,
        batch_status=(
            "closed"
            if batch.status == ImportBatchStatus.CLOSED
            else "open"
        ),
    )
