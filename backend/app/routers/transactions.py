import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models.account import Account
from app.models.transaction import TransactionType
from app.models.user import User
from app.schemas.transaction import (
    BulkDeleteRequest,
    BulkDeleteResponse,
    ConvertToTransferRequest,
    TransactionCreate,
    TransactionPairRequest,
    TransactionResponse,
    TransactionUpdate,
    TransferCandidate,
    TransferCandidatesResponse,
    TransferCreate,
    UnpairTransactionRequest,
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
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
):
    txns = await svc.list_transactions(
        db, current_user.org_id,
        account_id=account_id,
        category_id=category_id,
        tx_type=tx_type,
        status=status,
        date_from=date_from,
        date_to=date_to,
        search=search,
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
