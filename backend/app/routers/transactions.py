import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models.account import Account
from app.models.transaction import TransactionType
from app.models.user import User
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
from app.services import transaction_service as svc
from app.services.exceptions import NotFoundError, ValidationError

router = APIRouter(prefix="/api/v1/transactions", tags=["transactions"])


@router.get("", response_model=list[TransactionResponse])
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
    limit: int = Query(default=50, le=200),
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
    txns = await svc.list_transactions(
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
        limit=limit,
        offset=offset,
    )
    return [svc.to_response(tx) for tx in txns]


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
    status_code=501,
)
async def batch_create_transactions(
    body: BatchTransactionsRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """**STUB** — Manual batch transaction entry, Wave 2 deliverable.

    Contract: accept ``list[TransactionCreate]`` (wrapped in
    ``BatchTransactionRow`` with a stable ``row_number``), process each
    row in its own savepoint, return per-row results + aggregate counters.
    Rows are NOT flagged ``is_imported`` — they're user-typed, not bank
    sourced.

    Frozen contract: see spec at
    ``~/.claude/projects/-Users-fjorge-src-pfv/specs/2026-05-12-l3-2-import-contracts.md``
    §0.2 (Manual batch entry shape) and ``app/schemas/import_batch.py``.

    Wave 2 Manual Batch Entry team owns implementation.
    """
    _ = (current_user.org_id, db, len(body.rows))
    raise HTTPException(
        status_code=501,
        detail=(
            "Batch transaction entry not implemented — see L3.2 dispatch "
            "(specs/2026-05-12-l3-2-import-contracts.md §0.2)"
        ),
    )


@router.get(
    "/suggestions/descriptions",
    response_model=DescriptionSuggestionsResponse,
    status_code=501,
)
async def suggest_descriptions(
    type: Literal["income", "expense", "transfer"] = Query(...),
    q: str | None = Query(default=None, min_length=2, max_length=255),
    limit: int = Query(default=10, ge=1, le=25),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """**STUB** — Description autocomplete, Wave 2 deliverable.

    Contract: return the user's most-used descriptions for ``type``,
    ranked by prefix-match → frequency → recency. Org-scoped (never
    leaks across orgs). Never logs raw descriptions or raw query strings.

    When ``q`` is omitted, returns top-N most-used descriptions (useful
    for the manual-entry form's "recent descriptions" hint).

    Frozen contract: see spec at
    ``~/.claude/projects/-Users-fjorge-src-pfv/specs/2026-05-12-l3-2-import-contracts.md``
    §5 (Description Suggestions Contract) and
    ``app/schemas/transaction_suggestions.py``.

    Wave 2 Description Suggestions team owns implementation. Frontend
    is expected to debounce 300 ms and skip requests when q.length < 2.
    """
    _ = (current_user.org_id, db, type, q, limit)
    raise HTTPException(
        status_code=501,
        detail=(
            "Description suggestions not implemented — see L3.2 dispatch "
            "(specs/2026-05-12-l3-2-import-contracts.md §5)"
        ),
    )


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
