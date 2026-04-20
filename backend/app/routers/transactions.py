import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.schemas.transaction import (
    BulkDeleteRequest,
    BulkDeleteResponse,
    TransactionCreate,
    TransactionResponse,
    TransactionUpdate,
    TransferCreate,
)
from app.services import transaction_service as svc

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
    deleted_count, skipped_ids = await svc.bulk_delete_transactions(
        db, current_user.org_id, body.ids
    )
    return BulkDeleteResponse(
        requested_count=len(body.ids),
        deleted_count=deleted_count,
        skipped_ids=skipped_ids,
    )
