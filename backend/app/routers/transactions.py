import datetime
from typing import Literal, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models.account import Account
from app.models.transaction import TransactionType
from app.models.user import User
from app.rate_limit import get_client_ip
from app.schemas.common import ListEnvelope
from app.schemas.import_batch import (
    BatchTransactionsRequest,
    BatchTransactionsResponse,
)
from app.schemas.transaction import (
    BulkDeleteRequest,
    BulkDeleteResponse,
    ConvertToTransferRequest,
    PromoteToRecurringRequest,
    TransactionCreate,
    TransactionPairRequest,
    TransactionResponse,
    TransactionUpdate,
    TransferCandidate,
    TransferCandidatesResponse,
    TransferCreate,
    UnpairTransactionRequest,
)
from app.schemas.transaction_suggestions import (
    DescriptionSuggestionsResponse,
)
from app.services import (
    audit_service,
    transaction_batch_service,
    transaction_service as svc,
    transaction_suggestions_service as suggestions_svc,
)
from app.services.exceptions import NotFoundError, ValidationError


def _request_id() -> Optional[str]:
    """Return the structlog-bound request_id, if any."""
    return structlog.contextvars.get_contextvars().get("request_id")


# Dedicated logger for the suggestions endpoint so its events are easy
# to filter in structured logs without colliding with general
# transactions handlers. Privacy rules in §5.4 of the L3.2 import
# contract: we log only org_id, type, query_length, result_count,
# never the raw ``q`` or returned descriptions.
suggestions_logger = structlog.stdlib.get_logger(
    "app.routers.transactions.suggestions"
)

router = APIRouter(prefix="/api/v1/transactions", tags=["transactions"])


@router.get("", response_model=ListEnvelope[TransactionResponse])
async def list_transactions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    account_id: int | None = Query(default=None),
    category_id: int | None = Query(default=None),
    tx_type: Literal["income", "expense"] | None = Query(default=None, alias="type"),
    status: Literal["settled", "pending"] | None = Query(default=None),
    date_from: datetime.date | None = Query(default=None),
    date_to: datetime.date | None = Query(default=None),
    search: str | None = Query(default=None),
    tags: str | None = Query(
        default=None,
        description=(
            "Comma-separated tag names. Default semantics: AND (all "
            "named tags must be attached). Set tag_match=any for OR."
        ),
    ),
    tags_exclude: str | None = Query(
        default=None,
        description=(
            "Comma-separated tag names. Transactions tagged with ANY "
            "of these are excluded."
        ),
    ),
    tag_match: Literal["all", "any"] = Query(
        default="all",
        description=(
            "Match mode for the ``tags`` filter. 'all' (default) "
            "requires every named tag; 'any' requires at least one."
        ),
    ),
    sort_by: str | None = Query(default=None),
    sort_dir: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    tag_list = (
        [t for t in (s.strip() for s in tags.split(",")) if t]
        if tags else None
    )
    excl_list = (
        [t for t in (s.strip() for s in tags_exclude.split(",")) if t]
        if tags_exclude else None
    )
    try:
        items, total = await svc.list_transactions(
            db, current_user.org_id,
            account_id=account_id,
            category_id=category_id,
            tx_type=tx_type,
            status=status,
            date_from=date_from,
            date_to=date_to,
            search=search,
            tags=tag_list,
            tags_exclude=excl_list,
            tag_match=tag_match,
            sort_by=sort_by,
            sort_dir=sort_dir,
            limit=limit,
            offset=offset,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc
    return ListEnvelope[TransactionResponse](
        items=[svc.to_response(tx) for tx in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=TransactionResponse, status_code=201)
async def create_transaction(
    body: TransactionCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tx = await svc.create_transaction(db, current_user.org_id, body)
    return svc.to_response(tx)


@router.post("/transfer", response_model=list[TransactionResponse], status_code=201)
async def create_transfer(
    body: TransferCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tx1, tx2 = await svc.create_transfer(db, current_user.org_id, body)
    return [svc.to_response(tx1), svc.to_response(tx2)]


@router.post("/pair", response_model=list[TransactionResponse], status_code=201)
async def pair_transactions(
    body: TransactionPairRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    expense_tx, income_tx = await svc.pair_existing_transactions(
        db,
        current_user.org_id,
        expense_tx_id=body.expense_id,
        income_tx_id=body.income_id,
        recategorize=body.recategorize,
        transfer_category_id=body.transfer_category_id,
    )
    pair = sorted(
        [svc.to_response(expense_tx), svc.to_response(income_tx)],
        key=lambda r: r.id,
    )
    return pair


@router.post(
    "/{transaction_id}/convert-to-transfer",
    response_model=list[TransactionResponse],
    status_code=201,
)
async def convert_to_transfer(
    transaction_id: int,
    body: ConvertToTransferRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.pair_with_transaction_id is not None:
        # Validate the candidate's account matches destination_account_id.
        partner = await svc.get_transaction(
            db, current_user.org_id, body.pair_with_transaction_id
        )
        if partner.account_id != body.destination_account_id:
            raise ValidationError(
                "pair_with_transaction_id account does not match destination_account_id"
            )
        source = await svc.get_transaction(db, current_user.org_id, transaction_id)
        if source.type == TransactionType.EXPENSE:
            expense_id, income_id = source.id, partner.id
        else:
            expense_id, income_id = partner.id, source.id
        e, i = await svc.pair_existing_transactions(
            db,
            current_user.org_id,
            expense_tx_id=expense_id,
            income_tx_id=income_id,
            recategorize=body.recategorize,
            transfer_category_id=body.transfer_category_id,
        )
    else:
        e, i = await svc.convert_and_create_leg(
            db,
            current_user.org_id,
            transaction_id,
            destination_account_id=body.destination_account_id,
            recategorize=body.recategorize,
            transfer_category_id=body.transfer_category_id,
        )
        # convert_and_create_leg only refreshes partner.account, not category.
        # Re-fetch both legs with full eager loads so to_response can serialize.
        e = await svc.get_transaction(db, current_user.org_id, e.id)
        i = await svc.get_transaction(db, current_user.org_id, i.id)
    pair = sorted([svc.to_response(e), svc.to_response(i)], key=lambda r: r.id)
    return pair


@router.post(
    "/{transaction_id}/unpair",
    response_model=list[TransactionResponse],
    status_code=200,
)
async def unpair_transaction(
    transaction_id: int,
    body: UnpairTransactionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    e, i = await svc.unpair_transactions(
        db,
        current_user.org_id,
        transaction_id,
        expense_fallback_category_id=body.expense_fallback_category_id,
        income_fallback_category_id=body.income_fallback_category_id,
    )
    pair = sorted([svc.to_response(e), svc.to_response(i)], key=lambda r: r.id)
    return pair


@router.get(
    "/{transaction_id}/transfer-candidates",
    response_model=TransferCandidatesResponse,
)
async def transfer_candidates(
    transaction_id: int,
    destination_account_id: int = Query(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    source = await svc.get_transaction(db, current_user.org_id, transaction_id)
    dst_acct = await db.scalar(
        select(Account).where(
            Account.id == destination_account_id,
            Account.org_id == current_user.org_id,
        )
    )
    if dst_acct is None:
        raise NotFoundError("Account")
    if source.account.currency != dst_acct.currency:
        raise ValidationError("Source and destination must have the same currency")
    candidates = await svc.find_match_candidates(
        db,
        current_user.org_id,
        source_type=source.type,
        amount=source.amount,
        account_id_excluded=source.account_id,
        date=source.date,
        currency=source.account.currency,
    )
    out = []
    for c in candidates:
        if c.account_id != destination_account_id:
            continue
        diff = abs((c.date - source.date).days)
        out.append(
            TransferCandidate(
                id=c.id,
                date=c.date,
                description=c.description,
                amount=c.amount,
                account_id=c.account_id,
                account_name=c.account.name,
                date_diff_days=diff,
                confidence="same_day" if diff == 0 else "near_date",
            )
        )
    return TransferCandidatesResponse(candidates=out)


# ── L3.2 Wave 1 contract stubs ───────────────────────────────────────────────
# Both routes MUST appear before ``/{transaction_id}`` so FastAPI's path
# matcher resolves the literal segments first. Returning 501 with the
# proper Pydantic response models keeps the OpenAPI schema honest for
# Wave 2 downstream teams.


@router.post(
    "/batch",
    response_model=BatchTransactionsResponse,
    status_code=200,
)
async def batch_create_transactions(
    body: BatchTransactionsRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(
        get_session_factory
    ),
):
    """Manual batch transaction entry (L3.2 Wave 2A).

    Accepts up to 500 rows, each wrapped in ``BatchTransactionRow`` with
    a stable ``row_number``. Each row is processed inside its own
    savepoint; failures roll back that one savepoint and surface as a
    ``BatchRowError`` while surviving rows commit on the outer
    transaction at the end of the request.

    Rows are user-typed, NOT imported — ``is_imported=False``. Per-row
    validation mirrors the single-row ``POST /api/v1/transactions``
    path (account + category org-scope, category-type compatibility,
    amount/date bounds enforced by the shared ``TransactionCreate``
    schema). Duplicate ``row_number`` values are rejected with 422 at
    schema validation time (validator on
    ``BatchTransactionsRequest``).

    One audit event is emitted per batch invocation summarizing
    imported / error counts. The audit row is written in its own
    transaction (``record_audit_event``) so it persists even if the
    outer commit fails midway — matching the audit semantics elsewhere
    in the codebase.

    Frozen contract: see spec at
    ``~/.claude/projects/-Users-fjorge-src-pfv/specs/2026-05-12-l3-2-import-contracts.md``
    §0.2 and ``app/schemas/import_batch.py``.
    """
    response = await transaction_batch_service.create_batch(
        db, current_user.org_id, body
    )
    await db.commit()

    # One audit event per batch invocation. Carries counters + the
    # length of the input list so a future regression in the request
    # parser (e.g. silent row truncation) is visible in the audit log.
    await audit_service.record_audit_event(
        session_factory,
        event_type="transactions.batch_create",
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        target_org_id=current_user.org_id,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success" if response.error_count == 0 else "failure",
        detail={
            "row_count": len(body.rows),
            "imported_count": response.imported_count,
            "error_count": response.error_count,
        },
    )

    return response


@router.get(
    "/suggestions/descriptions",
    response_model=DescriptionSuggestionsResponse,
)
async def suggest_descriptions(
    response: Response,
    type: Literal["income", "expense", "transfer"] = Query(...),
    q: str | None = Query(default=None, min_length=2, max_length=255),
    limit: int = Query(default=10, ge=1, le=25),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Description autocomplete for the manual-entry form (L3.2 Wave 2A).

    Returns the user's most-used descriptions for ``type``, ranked by
    prefix-match → frequency → recency. Org-scoped — never leaks
    across orgs. When ``q`` is omitted, returns the top-N most-used
    descriptions for ``type`` (useful for the "recent descriptions"
    hint on an empty input).

    Privacy (§5.4): the raw query string ``q`` and the returned
    descriptions MUST NOT appear in logs. We log only ``org_id``,
    ``type``, ``query_length``, ``result_count``.

    Cache: ``Cache-Control: private, max-age=60`` — per-user, no shared
    CDN cache (§5.4).

    Frozen contract: see spec at
    ``~/.claude/projects/-Users-fjorge-src-pfv/specs/2026-05-12-l3-2-import-contracts.md``
    §5 and ``app/schemas/transaction_suggestions.py``.
    """
    suggestions = await suggestions_svc.get_description_suggestions(
        db,
        org_id=current_user.org_id,
        type=type,
        q=q,
        limit=limit,
    )
    # No-PII log line (§5.4): never log raw descriptions or the raw
    # query string. ``query_length`` is the only signal we keep about
    # ``q``; ``result_count`` reflects what we're returning.
    suggestions_logger.info(
        "transactions.suggestions.descriptions",
        org_id=current_user.org_id,
        type=type,
        query_length=len(q) if q else 0,
        result_count=len(suggestions),
    )
    response.headers["Cache-Control"] = "private, max-age=60"
    return DescriptionSuggestionsResponse(suggestions=suggestions)


@router.get("/{transaction_id}", response_model=TransactionResponse)
async def get_transaction(
    transaction_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tx = await svc.get_transaction(db, current_user.org_id, transaction_id)
    return svc.to_response(tx)


@router.put("/{transaction_id}", response_model=TransactionResponse)
async def update_transaction(
    transaction_id: int,
    body: TransactionUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tx = await svc.update_transaction(db, current_user.org_id, transaction_id, body)
    return svc.to_response(tx)


@router.post(
    "/{transaction_id}/promote-to-recurring",
    response_model=TransactionResponse,
    status_code=201,
)
async def promote_transaction_to_recurring(
    transaction_id: int,
    body: PromoteToRecurringRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Promote an existing transaction into a recurring template.

    Creates a recurring template with the transaction's account, category,
    amount, type, and description, plus the supplied frequency and next due
    date. Sets the transaction's recurring_id to point at the new template.
    Atomic in a single DB transaction.

    Out of scope: transfer legs (returns 400) and re-promotion of an
    already-recurring transaction (returns 400).
    """
    tx = await svc.promote_to_recurring(
        db, current_user.org_id, transaction_id, body
    )
    return svc.to_response(tx)


@router.delete("/{transaction_id}", status_code=204)
async def delete_transaction(
    transaction_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await svc.delete_transaction(db, current_user.org_id, transaction_id)


@router.post("/bulk-delete", response_model=BulkDeleteResponse)
async def bulk_delete_transactions(
    body: BulkDeleteRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete multiple transactions atomically.

    Cross-org IDs are silently skipped. Transfer-pair halves cascade.
    Cap: 500 IDs per request (enforced by Pydantic).
    """
    unique_ids = list(dict.fromkeys(body.ids))
    deleted_count, skipped_ids = await svc.bulk_delete_transactions(
        db, current_user.org_id, unique_ids
    )
    return BulkDeleteResponse(
        requested_count=len(unique_ids),
        deleted_count=deleted_count,
        skipped_ids=skipped_ids,
    )
