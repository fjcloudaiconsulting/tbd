from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.schemas.transaction import (
    TransactionCreate,
    TransactionResponse,
    TransactionUpdate,
)
from app.services import transaction_service as svc

router = APIRouter(prefix="/api/v1/transactions", tags=["transactions"])


@router.get("", response_model=list[TransactionResponse])
async def list_transactions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    account_id: int | None = Query(default=None),
    category_id: int | None = Query(default=None),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
):
    txns = await svc.list_transactions(
        db, current_user.org_id, account_id, category_id, limit, offset
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
