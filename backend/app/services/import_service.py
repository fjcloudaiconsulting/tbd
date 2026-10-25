"""Import service — orchestrates CSV preview and transaction creation.

Parsing/validation is separate from persistence so a background worker
can replace the synchronous confirm path later without a rewrite.
"""

import hashlib
import uuid
from collections import Counter
from dataclasses import dataclass, field

import logging
from sqlalchemy import and_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.category import Category, CategoryType
from app.models.import_batch import ImportBatch, ImportBatchStatus, ImportSourceFormat
from app.models.settings import OrgSetting
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.schemas.import_schemas import (
    ImportConfirmRequest,
    ImportConfirmResponse,
    ImportConfirmRow,
    ImportPreviewRow,
    ImportPreviewResponse,
    ImportRowError,
)
from app.schemas.transaction import (
    DuplicateCandidate,
    TransactionCreate,
    TransferCandidate,
)
from app.services import transaction_service
from app.services.exceptions import (
    ConflictError,
    MissingCategoryTypeError,
    NotFoundError,
    ValidationError,
)
from app.services.transaction_service import (
    _create_transaction_no_commit,
    _link_pair,
    _load_opts,
    find_duplicate_of_linked_leg,
    find_duplicate_of_linked_leg_batch,
    find_match_candidates_batch,
    get_account_for_update,
)
from app.services.category_rules_service import (
    bump_shared_vote,
    infer_categories,
    learn_from_choice,
    normalize_description,
    should_skip_learning,
)
from app.services.import_parser import ParsedRow
from app import tracing

logger = logging.getLogger(__name__)


async def _assert_categories_cover_row_types(
    db: AsyncSession,
    org_id: int,
    parsed_rows: list[ParsedRow],
) -> None:
    """Raise ``MissingCategoryTypeError`` if the parsed rows include a type
    the org has no compatible category for.

    For each ``type`` value present in ``parsed_rows`` (``"expense"`` or
    ``"income"``), the org must have at least one category whose ``type``
    is either that exact value or ``CategoryType.BOTH``. Mirrors the
    frontend's category dropdown filter on the import preview page so the
    user never lands on a preview with no selectable default category.

    Architect-refined Category Fallback Layer B (post-L3.10): scope the
    requirement to what's actually in the file, not a blanket
    "income + expense" floor. Empty ``parsed_rows`` is treated as a no-op
    here; the upstream parser handles the "no rows" case separately.
    """
    present_types = {r.type for r in parsed_rows if r.type in ("expense", "income")}
    if not present_types:
        return

    # Single SELECT, group by category type. Keeps it to one round-trip
    # even when both types are present. ``BOTH`` covers either side.
    type_rows = (await db.execute(
        select(Category.type).where(Category.org_id == org_id)
    )).all()
    org_types = {row[0] for row in type_rows}

    def has_compatible(row_type: str) -> bool:
        if row_type == "expense":
            return CategoryType.EXPENSE in org_types or CategoryType.BOTH in org_types
        if row_type == "income":
            return CategoryType.INCOME in org_types or CategoryType.BOTH in org_types
        return True

    missing = sorted(t for t in present_types if not has_compatible(t))
    if not missing:
        return

    # Human-readable copy the frontend can surface as a fallback when it
    # doesn't recognize ``code``. No em-dashes (user-facing copy rule).
    if missing == ["expense"]:
        message = (
            "This import has expense rows but you have no expense category. "
            "Add one to continue."
        )
    elif missing == ["income"]:
        message = (
            "This import has income rows but you have no income category. "
            "Add one to continue."
        )
    else:
        message = (
            "This import needs income and expense categories, but your "
            "organization is missing one of each. Add them to continue."
        )

    logger.info(
        "import.preview.missing_category_type",
        extra={
            "org_id": org_id,
            "missing_types": missing,
        },
    )

    raise MissingCategoryTypeError(missing_types=missing, message=message)


async def build_preview(
    db: AsyncSession,
    org_id: int,
    account_id: int,
    file_name: str,
    parsed_rows: list[ParsedRow],
    *,
    source_format: str = "csv",
) -> ImportPreviewResponse:
    """Build a preview response: flag duplicates and potential transfers."""

    # Validate account belongs to this org and load it for currency.
    # Detectors filter cross-account candidates by Account.currency; we need
    # the destination's currency to keep the match space sensible.
    destination_account = await db.scalar(
        select(Account).where(Account.id == account_id, Account.org_id == org_id)
    )
    if destination_account is None:
        # Match validate_account semantics: surface the same error shape.
        await transaction_service.validate_account(db, account_id, org_id)

    # ── Layer B preflight: require a compatible category per row type ──
    # Architect-refined Category Fallback design (post-L3.10): only require
    # what the parsed rows actually need. Expense-only CSV doesn't require
    # an income category; vice versa for income-only. Raises
    # ``MissingCategoryTypeError`` before any duplicate / detector work so
    # the user gets a fast, targeted 400 on the upload step.
    await _assert_categories_cover_row_types(db, org_id, parsed_rows)

    # ── Batch duplicate check: single query for the CSV date range ──
    min_date = min(r.date for r in parsed_rows)
    max_date = max(r.date for r in parsed_rows)
    existing_result = await db.execute(
        select(
            Transaction.id, Transaction.date, Transaction.amount, Transaction.description
        ).where(
            and_(
                Transaction.org_id == org_id,
                Transaction.account_id == account_id,
                Transaction.date.between(min_date, max_date),
            )
        )
    )
    existing_map: dict[tuple, int] = {
        (row.date, row.amount, row.description): row.id
        for row in existing_result.all()
    }

    # ── FITID-aware dedup (L3.2 OFX path) ───────────────────────────────
    # Spec §2.1: when ``fitid`` is present on a parsed row, it overrides
    # the description-based 5-tuple match. Banks guarantee FITID
    # uniqueness **within an account** (OFX spec §11.4.4), so the
    # cross-batch lookup MUST scope by ``account_id`` as well -- two
    # different banks can legitimately emit the same FITID for unrelated
    # transactions, and an org-wide lookup would flag those as
    # duplicates (PR #247 P2 fix).
    #
    # The composite ``(org_id, fitid)`` index still covers the lookup;
    # the additional ``account_id`` filter is a constant-time
    # comparison after the index seek.
    parsed_fitids = [r.fitid for r in parsed_rows if getattr(r, "fitid", None)]
    existing_fitids: set[str] = set()
    if parsed_fitids:
        fitid_result = await db.execute(
            select(Transaction.fitid).where(
                and_(
                    Transaction.org_id == org_id,
                    Transaction.account_id == account_id,
                    Transaction.fitid.in_(parsed_fitids),
                )
            )
        )
        existing_fitids = {row.fitid for row in fitid_result.all() if row.fitid}
    seen_fitids: set[tuple[int, str]] = set()

    # ── Batched detectors (TBD-736) ──
    # Smart-rules suggestions and both transfer detectors resolve every row
    # with a fixed number of set-based queries instead of several per row.
    # Each lookup returns exactly what the per-row helper returns for the
    # same inputs (fenced in tests/services/test_import_preview_batching.py).
    def _tx_type(row: ParsedRow) -> TransactionType:
        return TransactionType.EXPENSE if row.type == "expense" else TransactionType.INCOME

    detector_keys = {(_tx_type(r), r.amount, r.date) for r in parsed_rows}
    suggestions = await infer_categories(
        db, org_id=org_id, descriptions=[r.description for r in parsed_rows]
    )
    dup_linked_by_key = await find_duplicate_of_linked_leg_batch(
        db, org_id,
        account_id=account_id,
        currency=destination_account.currency,
        keys=detector_keys,
    )
    match_candidates_by_key = await find_match_candidates_batch(
        db, org_id,
        account_id_excluded=account_id,
        currency=destination_account.currency,
        keys=detector_keys,
    )

    preview_rows: list[ImportPreviewRow] = []
    duplicate_count = 0

    for row in parsed_rows:
        dup_id = existing_map.get((row.date, row.amount, row.description))
        is_dup = dup_id is not None

        # FITID arm of the dedup key (spec §2.1). When the parsed row
        # carries a FITID and we've already seen the same
        # (account_id, fitid) in this batch, mark the second occurrence
        # as a duplicate regardless of date / amount / description drift.
        # L3.2 Wave 2B: also marks the row as duplicate when the FITID
        # already exists in the org's transactions table from a previous
        # import (cross-batch dedup).
        row_fitid: str | None = getattr(row, "fitid", None)
        if row_fitid:
            if row_fitid in existing_fitids:
                is_dup = True
            fitid_key = (account_id, row_fitid)
            if fitid_key in seen_fitids:
                is_dup = True
            else:
                seen_fitids.add(fitid_key)

        # ── Smart-rules suggestion ──
        suggested_category_id, suggestion_source = suggestions[row.description]

        if is_dup:
            duplicate_count += 1

        # ── Detector 1: same-account already-linked match → flag duplicate-of-linked-leg ──
        row_key = (_tx_type(row), row.amount, row.date)
        dup_linked_candidates = dup_linked_by_key[row_key]
        is_dup_of_linked = bool(dup_linked_candidates)
        duplicate_candidate_obj: DuplicateCandidate | None = None
        if is_dup_of_linked:
            c0 = dup_linked_candidates[0]
            duplicate_candidate_obj = DuplicateCandidate(
                id=c0.id,
                date=c0.date,
                description=c0.description,
                amount=c0.amount,
                account_id=c0.account_id,
                account_name=c0.account.name,
                existing_leg_is_imported=c0.is_imported,
            )

        # ── Detector 2: cross-account un-linked match → suggest transfer pair ──
        # Skipped when Detector 1 fires — a duplicate-of-linked-leg already has
        # an answer (default action: drop), so cross-account suggestions would
        # only confuse the user.
        if is_dup_of_linked:
            action: str = "none"
            confidence: str | None = None
            pair_with_id: int | None = None
            candidate_models: list[TransferCandidate] = []
        else:
            match_candidates = match_candidates_by_key[row_key]
            if not match_candidates:
                action, confidence, pair_with_id, candidate_models = (
                    "none", None, None, [],
                )
            elif len(match_candidates) >= 2:
                action = "choose_candidate"
                confidence = "multi_candidate"
                pair_with_id = None
                candidate_models = [
                    TransferCandidate(
                        id=c.id,
                        date=c.date,
                        settled_date=c.settled_date,
                        description=c.description,
                        amount=c.amount,
                        account_id=c.account_id,
                        account_name=c.account.name,
                        date_diff_days=abs((c.date - row.date).days),
                        confidence="same_day" if c.date == row.date else "near_date",
                    )
                    for c in match_candidates
                ]
            else:
                c0 = match_candidates[0]
                diff = abs((c0.date - row.date).days)
                if diff == 0:
                    action, confidence = "pair_with", "same_day"
                else:
                    action, confidence = "suggest_pair", "near_date"
                pair_with_id = c0.id
                candidate_models = []

        preview_rows.append(
            ImportPreviewRow(
                row_number=row.row_number,
                date=row.date,
                description=row.description,
                amount=row.amount,
                type=row.type,
                counterparty=row.counterparty,
                transaction_type=row.transaction_type,
                is_duplicate=is_dup,
                duplicate_transaction_id=dup_id,
                suggested_category_id=suggested_category_id,
                suggestion_source=suggestion_source,
                # Detector 1 outputs.
                is_duplicate_of_linked_leg=is_dup_of_linked,
                duplicate_candidate=duplicate_candidate_obj,
                default_action_drop=is_dup_of_linked,
                # Detector 2 outputs.
                transfer_match_action=action,
                transfer_match_confidence=confidence,
                pair_with_transaction_id=pair_with_id,
                transfer_candidates=candidate_models,
                # OFX-only extras (None on CSV path).
                fitid=getattr(row, "fitid", None),
                bank_id=getattr(row, "bank_id", None),
                account_type_ofx=getattr(row, "account_type_ofx", None),
            )
        )

    # ── Aggregate smart-rules metric (architect-mandated; one event per preview) ──
    source_split = Counter((r.suggestion_source or "skipped") for r in preview_rows)
    suggested_count = sum(
        1 for r in preview_rows if r.suggested_category_id is not None
    )
    logger.info(
        "smart_rules.preview_built",
        extra={
            "org_id": org_id,
            "rows_total": len(preview_rows),
            "suggested_count": suggested_count,
            "source_split": dict(source_split),
        },
    )

    # ── Detector summary counters + telemetry (spec §3.2) ──
    auto_paired_count = sum(
        1 for r in preview_rows if r.transfer_match_action == "pair_with"
    )
    suggested_pair_count = sum(
        1 for r in preview_rows if r.transfer_match_action == "suggest_pair"
    )
    multi_candidate_count = sum(
        1 for r in preview_rows if r.transfer_match_action == "choose_candidate"
    )
    duplicate_of_linked_count = sum(
        1 for r in preview_rows if r.is_duplicate_of_linked_leg
    )
    logger.info(
        "import.preview.matched",
        extra={
            "org_id": org_id,
            "file_name": file_name,
            "auto_paired": auto_paired_count,
            "suggested": suggested_pair_count,
            "multi_candidate": multi_candidate_count,
            "duplicate_of_linked": duplicate_of_linked_count,
        },
    )

    return ImportPreviewResponse(
        rows=preview_rows,
        account_id=account_id,
        file_name=file_name,
        total_rows=len(preview_rows),
        duplicate_count=duplicate_count,
        auto_paired_count=auto_paired_count,
        suggested_pair_count=suggested_pair_count,
        multi_candidate_count=multi_candidate_count,
        duplicate_of_linked_count=duplicate_of_linked_count,
        source_format=source_format,
    )


# ── Confirm (TBD-737: chunked commits, idempotent by key) ───────────────────
#
# A confirm commits every ``CONFIRM_CHUNK_ROWS`` rows instead of holding one
# transaction (and its account row locks) for the whole file. Its progress
# lives on the ``import_batches`` header it creates, which carries the
# client's ``idempotency_key``:
#
# 1. Claim: insert the header (key + request hash, ``next_row`` 0) and commit
#    it on its own. A second request with the same key hits the unique index
#    and attaches to the existing header instead; a different request body
#    under the same key is a 409.
# 2. Chunks: each chunk locks the header (``SELECT ... FOR UPDATE``), reads
#    ``next_row``, applies the next slice of rows, links the new transactions
#    to the header and writes the advanced progress, all in ONE transaction.
#    A chunk therefore lands whole (rows, balances, progress) or not at all,
#    and two requests for the same key can never apply the same slice: the
#    second waits on the lock, then sees the advanced ``next_row``.
# 3. Completion: the last chunk stores the final response. A repeated confirm
#    returns it (``replayed=True``) without touching any row. A confirm that
#    failed between chunks is resumed by repeating it with the same key.
#
# A confirm that imported nothing removes its header (no empty batches, as
# before); repeating it re-runs rows that wrote nothing the first time.

CONFIRM_CHUNK_ROWS = 500


@dataclass
class _ConfirmContext:
    org_id: int
    body: ImportConfirmRequest
    currency: str
    share_merchant_data: bool
    # Per-call telemetry (rows this call processed).
    learned_count: int = 0
    accepted_count: int = 0
    overridden_count: int = 0
    source_split: Counter = field(default_factory=Counter)
    miss_tokens: set[str] = field(default_factory=set)
    rows_processed: int = 0


@dataclass
class _ChunkResult:
    imported_count: int = 0
    paired_count: int = 0
    dropped_duplicate_count: int = 0
    skipped_count: int = 0
    errors: list[ImportRowError] = field(default_factory=list)
    imported_transaction_ids: list[int] = field(default_factory=list)


def _confirm_request_hash(body: ImportConfirmRequest) -> str:
    """Fingerprint of a confirm's content, key excluded."""
    payload = body.model_dump_json(exclude={"idempotency_key"})
    return hashlib.sha256(payload.encode()).hexdigest()


def _empty_response() -> ImportConfirmResponse:
    return ImportConfirmResponse(
        imported_count=0, skipped_count=0, error_count=0, errors=[],
    )


def _add_chunk(acc: ImportConfirmResponse, chunk: _ChunkResult) -> ImportConfirmResponse:
    errors = [*acc.errors, *chunk.errors]
    return acc.model_copy(update={
        "imported_count": acc.imported_count + chunk.imported_count,
        "paired_count": acc.paired_count + chunk.paired_count,
        "dropped_duplicate_count": (
            acc.dropped_duplicate_count + chunk.dropped_duplicate_count
        ),
        "skipped_count": acc.skipped_count + chunk.skipped_count,
        "errors": errors,
        "error_count": len(errors),
    })


async def _find_keyed_batch(
    db: AsyncSession, org_id: int, key: str
) -> ImportBatch | None:
    return await db.scalar(
        select(ImportBatch)
        .where(ImportBatch.org_id == org_id, ImportBatch.idempotency_key == key)
        .execution_options(populate_existing=True)
    )


async def _claim_confirm(
    db: AsyncSession,
    *,
    org_id: int,
    user_id: int,
    body: ImportConfirmRequest,
    key: str,
    request_hash: str,
) -> tuple[int, bool]:
    """Return ``(batch_id, attached)`` for this key, creating the header if new.

    ``attached`` is True when a header for the key already existed, i.e. this
    request repeats an earlier one.
    """
    existing = await _find_keyed_batch(db, org_id, key)
    if existing is None:
        batch = ImportBatch(
            org_id=org_id,
            account_id=body.account_id,
            source_format=ImportSourceFormat(body.source_format),
            file_name=body.file_name,
            created_by_user_id=user_id,
            status=ImportBatchStatus.OPEN,
            row_count=0,
            accepted_count=0,
            pending_count=0,
            idempotency_key=key,
            confirm_state={
                "request_hash": request_hash,
                "next_row": 0,
                "completed": False,
                "response": _empty_response().model_dump(mode="json"),
            },
        )
        db.add(batch)
        try:
            await db.flush()
            batch_id = batch.id
            await db.commit()
            return batch_id, False
        except IntegrityError:
            # A concurrent request with the same key claimed it first.
            await db.rollback()
            existing = await _find_keyed_batch(db, org_id, key)
            if existing is None:
                raise
    state = existing.confirm_state or {}
    if state.get("request_hash") != request_hash:
        raise ConflictError(
            "This import key was already used for a different import; "
            "upload the file again to start a new import"
        )
    batch_id = existing.id
    # Release the read snapshot so the chunk loop's locking reads see the
    # latest committed progress.
    await db.commit()
    return batch_id, True


async def execute_import(
    db: AsyncSession,
    org_id: int,
    body: ImportConfirmRequest,
    *,
    user_id: int | None = None,
) -> ImportConfirmResponse:
    """Create / pair / drop transactions for all confirmed (non-skipped) rows.

    Per-row behaviour is in ``_apply_confirm_rows``. This function owns the
    chunked commits and the idempotency described above. Without a
    ``user_id`` (service-level callers in tests) there is no batch header to
    record progress on: rows are still committed per chunk, but a repeat is
    not detected.
    """
    # Destination account for currency lookups (used by both pair partner
    # validation and drop_as_duplicate revalidation).
    destination_account = await db.scalar(
        select(Account).where(
            Account.id == body.account_id, Account.org_id == org_id
        )
    )
    if destination_account is None:
        await transaction_service.validate_account(db, body.account_id, org_id)

    # ── Smart-rules learning: fetch share flag once ──
    share_flag = (await db.execute(
        select(OrgSetting.value).where(
            OrgSetting.org_id == org_id,
            OrgSetting.key == "share_merchant_data",
        )
    )).scalar_one_or_none()

    ctx = _ConfirmContext(
        org_id=org_id,
        body=body,
        currency=destination_account.currency,
        share_merchant_data=(share_flag == "true"),
    )

    if user_id is None:
        response = await _run_unkeyed(db, ctx)
        attached = False
    else:
        key = body.idempotency_key or uuid.uuid4().hex
        request_hash = _confirm_request_hash(body)
        while True:
            batch_id, attached = await _claim_confirm(
                db, org_id=org_id, user_id=user_id, body=body,
                key=key, request_hash=request_hash,
            )
            response = await _run_keyed(db, ctx, batch_id)
            if response is not None:
                break
            # The header vanished under us: a concurrent request with this
            # key finished having imported nothing and removed it. Nothing
            # was written, so claim afresh and run the rows again.

    if attached and ctx.rows_processed == 0:
        logger.info(
            "import.confirm.replayed",
            extra={"org_id": org_id, "import_id": response.import_id},
        )
        return response.model_copy(update={"replayed": True})

    _log_confirm_telemetry(ctx, response)
    if attached:
        response = response.model_copy(update={"replayed": True})
    return response


async def _run_unkeyed(
    db: AsyncSession, ctx: _ConfirmContext
) -> ImportConfirmResponse:
    acc = _empty_response()
    rows = ctx.body.rows
    for start in range(0, len(rows), CONFIRM_CHUNK_ROWS):
        chunk = await _apply_confirm_rows(
            db, ctx, rows[start:start + CONFIRM_CHUNK_ROWS]
        )
        acc = _add_chunk(acc, chunk)
        await db.commit()
    return acc


async def _run_keyed(
    db: AsyncSession, ctx: _ConfirmContext, batch_id: int
) -> ImportConfirmResponse | None:
    """Drive the chunks of a claimed confirm to completion.

    Returns the final response, or ``None`` if the header disappeared (see
    ``execute_import``).
    """
    rows = ctx.body.rows
    while True:
        batch = await db.scalar(
            select(ImportBatch)
            .where(ImportBatch.id == batch_id, ImportBatch.org_id == ctx.org_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if batch is None:
            await db.commit()
            return None
        state = batch.confirm_state or {}
        acc = ImportConfirmResponse.model_validate(state["response"])
        if state.get("completed"):
            await db.commit()  # release the lock; nothing changed
            return acc

        start = int(state["next_row"])
        chunk = await _apply_confirm_rows(
            db, ctx, rows[start:start + CONFIRM_CHUNK_ROWS]
        )
        acc = _add_chunk(acc, chunk)
        next_row = min(start + CONFIRM_CHUNK_ROWS, len(rows))
        completed = next_row >= len(rows)

        new_ids = chunk.imported_transaction_ids
        if new_ids:
            await db.execute(
                update(Transaction)
                .where(
                    Transaction.id.in_(new_ids),
                    Transaction.org_id == ctx.org_id,
                )
                .values(import_batch_id=batch_id)
            )
            batch.row_count += len(new_ids)
            batch.accepted_count += len(new_ids)

        row_count = batch.row_count
        if completed and row_count == 0:
            # Nothing imported: keep the "no empty batches" rule.
            await db.delete(batch)
            await db.commit()
            return acc.model_copy(update={"import_id": None})

        acc = acc.model_copy(update={"import_id": batch_id if row_count else None})
        batch.confirm_state = {
            "request_hash": state["request_hash"],
            "next_row": next_row,
            "completed": completed,
            "response": acc.model_dump(mode="json"),
        }
        await db.commit()
        if completed:
            logger.info(
                "import.batch_created",
                extra={
                    "org_id": ctx.org_id,
                    "batch_id": batch_id,
                    "source_format": ctx.body.source_format,
                    "row_count": row_count,
                },
            )
            return acc


def _log_confirm_telemetry(
    ctx: _ConfirmContext, response: ImportConfirmResponse
) -> None:
    # ── Aggregate smart-rules metric (architect-mandated; one per import) ──
    # rows_total: rows this call processed (all submitted rows unless the
    # call resumed an interrupted confirm).
    logger.info(
        "smart_rules.import_executed",
        extra={
            "org_id": ctx.org_id,
            "rows_total": ctx.rows_processed,
            "imported_count": response.imported_count,
            "paired_count": response.paired_count,
            "learned_count": ctx.learned_count,
            "accepted_count": ctx.accepted_count,
            "overridden_count": ctx.overridden_count,
            "source_split": dict(ctx.source_split),
            "miss_count": len(ctx.miss_tokens),
        },
    )
    # Per-UNIQUE-token miss events (set-dedup is load-bearing — emitting
    # per-row would flood the metric with duplicates of the same merchant).
    for token in ctx.miss_tokens:
        logger.info(
            "smart_rules.miss",
            extra={
                "org_id": ctx.org_id,
                "normalized_token": token,
            },
        )

    # ── Per-execute confirm summary (transfer-aware) ──
    logger.info(
        "import.confirmed.transfers",
        extra={
            "org_id": ctx.org_id,
            "paired_count": response.paired_count,
            "dropped_duplicate_count": response.dropped_duplicate_count,
            "plain_created_count": response.imported_count,
        },
    )


async def _apply_confirm_rows(
    db: AsyncSession,
    ctx: _ConfirmContext,
    rows: list[ImportConfirmRow],
) -> _ChunkResult:
    """Create / pair / drop transactions for one slice of confirmed rows.

    Per-row branches (spec §3.3):
      action="create"             → plain insert via _create_transaction_no_commit.
      action="pair_with_existing" → insert new leg + atomically link to a
                                    locked partner via _link_pair. Single
                                    nested savepoint per row guarantees
                                    rollback on any failure inside the pair.
      action="drop_as_duplicate"  → server-side revalidate the duplicate-of-
                                    linked-leg candidate, then skip insert.

    Each row runs inside its own ``db.begin_nested()`` savepoint so failures
    in the pair branch (e.g. _link_pair raises after the new leg flushed)
    cleanly roll back the partial work without taking the whole slice with
    it. Smart-rules learning runs OUTSIDE the savepoint per row, best-effort:
    a learn failure must NOT bubble out as a row error or roll back the
    imported transaction. The caller owns the commit.
    """
    org_id = ctx.org_id
    body = ctx.body
    currency = ctx.currency
    share_merchant_data = ctx.share_merchant_data
    result = _ChunkResult()
    errors = result.errors
    # L3.2 Wave 2B: the IDs of every transaction this slice created, linked
    # to the ``import_batches`` header by the caller. The synthetic partner
    # leg of a create_transfer_pair is intentionally NOT collected (it
    # didn't come from the bank file). All other branches contribute their
    # bank-side leg ID.
    imported_transaction_ids = result.imported_transaction_ids
    imported_count = 0          # plain creates only (action="create")
    paired_count = 0
    dropped_duplicate_count = 0
    skipped_count = 0
    learned_count = 0
    accepted_count = 0
    overridden_count = 0
    source_split = ctx.source_split
    miss_tokens = ctx.miss_tokens
    paired_pair_ids: list[tuple[int, int]] = []

    for row in rows:
        if row.skip:
            skipped_count += 1
            continue

        category_id = row.category_id or body.default_category_id
        action_taken: str | None = None
        new_tx: Transaction | None = None

        try:
            async with db.begin_nested():
                if row.action == "create":
                    tx_body = TransactionCreate(
                        account_id=body.account_id,
                        category_id=category_id,
                        description=row.description,
                        amount=row.amount,
                        type=row.type,
                        date=row.date,
                    )
                    new_tx = await _create_transaction_no_commit(
                        db, org_id, tx_body, is_imported=True, fitid=row.fitid
                    )
                    action_taken = "create"

                elif row.action == "pair_with_existing":
                    if row.pair_with_transaction_id is None:
                        raise ValidationError(
                            "pair_with_transaction_id required when "
                            "action='pair_with_existing'"
                        )
                    tx_body = TransactionCreate(
                        account_id=body.account_id,
                        category_id=category_id,
                        description=row.description,
                        amount=row.amount,
                        type=row.type,
                        date=row.date,
                    )
                    new_tx = await _create_transaction_no_commit(
                        db, org_id, tx_body, is_imported=True, fitid=row.fitid
                    )

                    # Lock partner row with FOR UPDATE + populate_existing so
                    # the eligibility re-check reads the freshest server state
                    # (defends against a concurrent pair landing between
                    # preview and confirm).
                    partner_locked = await db.execute(
                        select(Transaction)
                        .options(*_load_opts())
                        .where(
                            Transaction.id == row.pair_with_transaction_id,
                            Transaction.org_id == org_id,
                        )
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    partner = partner_locked.scalar_one_or_none()
                    if partner is None:
                        raise ConflictError(
                            "Partner row no longer exists; re-preview"
                        )
                    if partner.linked_transaction_id is not None:
                        raise ConflictError(
                            "Partner row is already linked; re-preview"
                        )
                    if partner.recurring_id is not None:
                        raise ConflictError(
                            "Partner row is recurring; re-preview"
                        )
                    if partner.status != TransactionStatus.SETTLED:
                        # find_match_candidates only suggests SETTLED rows;
                        # if partner flipped to PENDING after preview, the
                        # pair invariant could break — bounce the user to
                        # re-preview.
                        raise ConflictError(
                            "Partner row no longer settled; re-preview"
                        )
                    if partner.account.currency != currency:
                        raise ConflictError(
                            "Currency mismatch detected at confirm; re-preview"
                        )

                    # Determine which leg is expense / income.
                    if new_tx.type == TransactionType.EXPENSE:
                        expense_tx, income_tx = new_tx, partner
                    else:
                        expense_tx, income_tx = partner, new_tx

                    await _link_pair(
                        db,
                        expense_tx=expense_tx,
                        income_tx=income_tx,
                        recategorize=row.recategorize,
                        transfer_category_id=row.transfer_category_id,
                    )
                    paired_pair_ids.append((new_tx.id, partner.id))
                    action_taken = "pair_with_existing"

                elif row.action == "drop_as_duplicate":
                    if row.duplicate_of_transaction_id is None:
                        raise ValidationError(
                            "duplicate_of_transaction_id required when "
                            "action='drop_as_duplicate'"
                        )
                    row_tx_type = (
                        TransactionType.EXPENSE
                        if row.type == "expense"
                        else TransactionType.INCOME
                    )
                    dup_check = await find_duplicate_of_linked_leg(
                        db, org_id,
                        account_id=body.account_id,
                        amount=row.amount,
                        type=row_tx_type,
                        date=row.date,
                        currency=currency,
                    )
                    candidate_ids = {c.id for c in dup_check}
                    if row.duplicate_of_transaction_id not in candidate_ids:
                        raise ConflictError(
                            "Duplicate candidate no longer matches; re-preview"
                        )
                    action_taken = "drop_as_duplicate"

                elif row.action == "create_transfer_pair":
                    # First-import case: partner side not yet in DB. Create
                    # BOTH legs (CSV leg + synthetic partner) and link them
                    # atomically inside this row's savepoint.
                    if row.partner_account_id is None:
                        raise ValidationError(
                            "partner_account_id required when "
                            "action='create_transfer_pair'"
                        )

                    # Validate partner: same org, different from import
                    # account, same currency.
                    partner_account = await db.scalar(
                        select(Account).where(
                            Account.id == row.partner_account_id,
                            Account.org_id == org_id,
                        )
                    )
                    if partner_account is None:
                        raise NotFoundError("Partner account")
                    if partner_account.id == body.account_id:
                        raise ValidationError(
                            "Partner account must differ from import account"
                        )
                    if partner_account.currency != currency:
                        raise ValidationError(
                            "Partner account currency must match import "
                            "account currency"
                        )

                    # Acquire account locks in sorted-ID order to prevent
                    # deadlock with concurrent imports running in the
                    # opposite direction (import A→B vs import B→A). The
                    # subsequent _create_transaction_no_commit calls will
                    # re-acquire the same locks inside this transaction;
                    # MySQL treats SELECT FOR UPDATE on an already-locked
                    # row by the same txn as a no-op.
                    first_id, second_id = sorted(
                        [body.account_id, row.partner_account_id]
                    )
                    await get_account_for_update(db, first_id, org_id)
                    await get_account_for_update(db, second_id, org_id)

                    # CSV leg — same shape as the plain create branch.
                    csv_body = TransactionCreate(
                        account_id=body.account_id,
                        category_id=category_id,
                        description=row.description,
                        amount=row.amount,
                        type=row.type,
                        date=row.date,
                    )
                    csv_leg = await _create_transaction_no_commit(
                        db, org_id, csv_body, is_imported=True, fitid=row.fitid
                    )

                    # Synthetic partner leg — opposite type, same magnitude
                    # / date / description / status. is_imported=False
                    # because the partner row was synthesized by us, not
                    # parsed from a bank export.
                    partner_type = "income" if row.type == "expense" else "expense"
                    partner_body = TransactionCreate(
                        account_id=row.partner_account_id,
                        category_id=category_id,
                        description=row.description,
                        amount=row.amount,
                        type=partner_type,
                        date=row.date,
                    )
                    partner_leg = await _create_transaction_no_commit(
                        db, org_id, partner_body, is_imported=False
                    )

                    # Determine which leg is expense / income for _link_pair.
                    if csv_leg.type == TransactionType.EXPENSE:
                        expense_tx, income_tx = csv_leg, partner_leg
                    else:
                        expense_tx, income_tx = partner_leg, csv_leg

                    # _link_pair's currency invariant requires .account
                    # loaded on both legs.
                    await db.refresh(csv_leg, attribute_names=["account"])
                    await db.refresh(partner_leg, attribute_names=["account"])

                    await _link_pair(
                        db,
                        expense_tx=expense_tx,
                        income_tx=income_tx,
                        recategorize=row.recategorize,
                        transfer_category_id=row.transfer_category_id,
                    )
                    paired_pair_ids.append((csv_leg.id, partner_leg.id))
                    # Bind for post-savepoint telemetry.
                    new_tx = csv_leg
                    action_taken = "create_transfer_pair"

        except (ConflictError, ValidationError, NotFoundError) as exc:
            # Domain failures: row-level error, batch continues.
            errors.append(
                ImportRowError(row_number=row.row_number, error=str(exc))
            )
            logger.warning(
                "import_row_failed",
                extra={
                    "row_number": row.row_number,
                    "error": tracing.error_summary(exc),
                    "error_type": type(exc).__name__,
                },
            )
            continue
        except Exception as exc:
            # Unexpected failures: surface as row-level error, batch continues.
            errors.append(
                ImportRowError(row_number=row.row_number, error=str(exc))
            )
            logger.warning(
                "import_row_failed",
                extra={
                    "row_number": row.row_number,
                    "error": tracing.error_summary(exc),
                    "error_type": type(exc).__name__,
                },
            )
            continue

        # L3.2 Wave 2B: collect the imported leg's ID. ``new_tx`` is the
        # row that came from the bank file -- the plain create row, the
        # newly-inserted leg in pair_with_existing, or the CSV leg in
        # create_transfer_pair. The synthetic partner leg in
        # create_transfer_pair is intentionally NOT linked. The
        # drop_as_duplicate branch never creates a row, so we skip.
        if (
            action_taken in ("create", "pair_with_existing", "create_transfer_pair")
            and new_tx is not None
        ):
            imported_transaction_ids.append(new_tx.id)

        # Per-action counter increments + telemetry (post-savepoint commit).
        if action_taken == "create":
            imported_count += 1
        elif action_taken == "pair_with_existing":
            paired_count += 1
            logger.info(
                "transfers.linked",
                extra={
                    "org_id": org_id,
                    "expense_id": expense_tx.id,
                    "income_id": income_tx.id,
                    "source": "import_pair",
                    "recategorized": row.recategorize,
                },
            )
        elif action_taken == "drop_as_duplicate":
            dropped_duplicate_count += 1
            logger.info(
                "import.dropped_duplicate_leg",
                extra={
                    "org_id": org_id,
                    "csv_row_index": row.row_number,
                    "duplicate_of_transaction_id": row.duplicate_of_transaction_id,
                    "account_id": body.account_id,
                    "amount": str(row.amount),  # spec §6.1: amount as string
                },
            )
        elif action_taken == "create_transfer_pair":
            # Counts as ONE CSV row (we created two legs but only one was in
            # the CSV — the partner is synthetic).
            paired_count += 1
            logger.info(
                "transfers.linked",
                extra={
                    "org_id": org_id,
                    "expense_id": expense_tx.id,
                    "income_id": income_tx.id,
                    "source": "import_create_pair",
                    "recategorized": row.recategorize,
                },
            )

        # ── Best-effort learning from the user's category choice ───────────
        # Runs OUTSIDE the per-row savepoint so a learn failure doesn't undo
        # the transaction. Skipped for paired rows (transfer legs — the
        # description is bank-noise, not a meaningful merchant) and dropped
        # rows (no transaction was created).
        #
        # Isolated in its OWN nested savepoint so a learn-side failure cannot
        # roll back successfully imported rows that share the outer
        # transaction. begin_nested() rolls back the savepoint automatically
        # on exception, leaving the outer transaction untouched.
        if action_taken == "create" and row.category_id is not None and not should_skip_learning(new_tx):
            accepted = (
                row.suggested_category_id is not None
                and row.suggested_category_id == row.category_id
            )
            source = "user_pick" if accepted else "user_edit"
            try:
                async with db.begin_nested():
                    await learn_from_choice(
                        db,
                        org_id=org_id,
                        description=row.description,
                        category_id=row.category_id,
                        source=source,
                    )
                    if (
                        accepted and share_merchant_data
                        and row.suggestion_source == "shared_dictionary"
                    ):
                        await bump_shared_vote(db, description=row.description)
                # savepoint released on normal exit; outer transaction unchanged
            except Exception as exc:
                # savepoint already rolled back by the begin_nested context
                # manager; outer transaction (with imports + paired rows)
                # untouched.
                logger.warning(
                    "smart_rules.learn_failed",
                    extra={
                        "org_id": org_id,
                        "op": "execute_import",
                        "row_number": row.row_number,
                        "error": tracing.error_summary(exc),
                        "error_type": type(exc).__name__,
                    },
                )

            # Counters always update — the user's choice was registered
            # against an imported row, even if the rule write failed.
            learned_count += 1
            if accepted:
                accepted_count += 1
            elif row.suggested_category_id is not None:
                overridden_count += 1

        # Metric collection — fires for EVERY imported plain-create row,
        # including default-category fallthroughs (row.category_id is None
        # and the user relied on default_category_id). Architect-mandated
        # "uncategorizable on import" signal; must NOT be gated on
        # row.category_id. Paired/dropped rows are excluded here because
        # paired rows aren't "missing a merchant rule" (they're transfers)
        # and dropped rows didn't create a transaction.
        if action_taken == "create" and not should_skip_learning(new_tx):
            source_split[row.suggestion_source or "default"] += 1
            if row.suggestion_source in ("default", None):
                token = normalize_description(row.description)
                if token:
                    miss_tokens.add(token)

    result.imported_count = imported_count
    result.paired_count = paired_count
    result.dropped_duplicate_count = dropped_duplicate_count
    result.skipped_count = skipped_count
    ctx.learned_count += learned_count
    ctx.accepted_count += accepted_count
    ctx.overridden_count += overridden_count
    ctx.rows_processed += len(rows)
    return result
