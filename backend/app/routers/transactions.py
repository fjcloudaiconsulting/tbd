from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.deps import get_current_user
from app.models.account import Account
from app.models.category import Category
from app.models.transaction import Transaction, TransactionType
from app.models.user import User
from app.schemas.transaction import (
    TransactionCreate,
    TransactionResponse,
    TransactionUpdate,
)

router = APIRouter(prefix="/api/v1/transactions", tags=["transactions"])


def _to_response(tx: Transaction) -> TransactionResponse:
    return TransactionResponse(
        id=tx.id,
        account_id=tx.account_id,
        account_name=tx.account.name if tx.account else "",
        category_id=tx.category_id,
        category_name=tx.category.name if tx.category else "",
        description=tx.description,
        amount=tx.amount,
        type=tx.type.value,
        date=tx.date,
    )


def _load_opts():
    return [selectinload(Transaction.account), selectinload(Transaction.category)]


async def _validate_category(
    db: AsyncSession, category_id: int, org_id: int
) -> None:
    cat = await db.scalar(
        select(Category.id).where(
            Category.id == category_id, Category.org_id == org_id
        )
    )
    if cat is None:
        raise HTTPException(status_code=400, detail="Invalid category")


async def _get_account_for_update(
    db: AsyncSession, account_id: int, org_id: int
) -> Account:
    result = await db.execute(
        select(Account)
        .where(Account.id == account_id, Account.org_id == org_id)
        .with_for_update()
    )
    acct = result.scalar_one_or_none()
    if acct is None:
        raise HTTPException(status_code=400, detail="Invalid account")
    return acct


@router.get("", response_model=list[TransactionResponse])
async def list_transactions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    account_id: int | None = Query(default=None),
    category_id: int | None = Query(default=None),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
):
    q = (
        select(Transaction)
        .options(*_load_opts())
        .where(Transaction.org_id == current_user.org_id)
    )
    if account_id is not None:
        q = q.where(Transaction.account_id == account_id)
    if category_id is not None:
        q = q.where(Transaction.category_id == category_id)
    q = q.order_by(Transaction.date.desc(), Transaction.id.desc())
    q = q.limit(limit).offset(offset)

    result = await db.execute(q)
    return [_to_response(tx) for tx in result.scalars().all()]


@router.post("", response_model=TransactionResponse, status_code=201)
async def create_transaction(
    body: TransactionCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _validate_category(db, body.category_id, current_user.org_id)

    tx_type = TransactionType(body.type)

    # _get_account_for_update validates existence + acquires row lock
    acct = await _get_account_for_update(db, body.account_id, current_user.org_id)
    if tx_type == TransactionType.INCOME:
        acct.balance += body.amount
    else:
        acct.balance -= body.amount

    tx = Transaction(
        org_id=current_user.org_id,
        account_id=body.account_id,
        category_id=body.category_id,
        description=body.description,
        amount=body.amount,
        type=tx_type,
        date=body.date,
    )
    db.add(tx)
    await db.commit()

    result = await db.execute(
        select(Transaction).options(*_load_opts()).where(Transaction.id == tx.id)
    )
    return _to_response(result.scalar_one())


@router.get("/{transaction_id}", response_model=TransactionResponse)
async def get_transaction(
    transaction_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Transaction)
        .options(*_load_opts())
        .where(
            Transaction.id == transaction_id,
            Transaction.org_id == current_user.org_id,
        )
    )
    tx = result.scalar_one_or_none()
    if tx is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return _to_response(tx)


@router.put("/{transaction_id}", response_model=TransactionResponse)
async def update_transaction(
    transaction_id: int,
    body: TransactionUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Transaction)
        .options(*_load_opts())
        .where(
            Transaction.id == transaction_id,
            Transaction.org_id == current_user.org_id,
        )
    )
    tx = result.scalar_one_or_none()
    if tx is None:
        raise HTTPException(status_code=404, detail="Transaction not found")

    # Revert old balance impact (with row lock)
    old_account = await _get_account_for_update(db, tx.account_id, current_user.org_id)
    if tx.type == TransactionType.INCOME:
        old_account.balance -= tx.amount
    else:
        old_account.balance += tx.amount

    if body.account_id is not None and body.account_id != tx.account_id:
        tx.account_id = body.account_id
    if body.category_id is not None:
        await _validate_category(db, body.category_id, current_user.org_id)
        tx.category_id = body.category_id
    if body.description is not None:
        tx.description = body.description
    if body.amount is not None:
        tx.amount = body.amount
    if body.type is not None:
        tx.type = TransactionType(body.type)
    if body.date is not None:
        tx.date = body.date

    # Apply new balance impact (with row lock)
    new_account = await _get_account_for_update(db, tx.account_id, current_user.org_id)
    if tx.type == TransactionType.INCOME:
        new_account.balance += tx.amount
    else:
        new_account.balance -= tx.amount

    await db.commit()

    result = await db.execute(
        select(Transaction).options(*_load_opts()).where(Transaction.id == tx.id)
    )
    return _to_response(result.scalar_one())


@router.delete("/{transaction_id}", status_code=204)
async def delete_transaction(
    transaction_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Transaction).where(
            Transaction.id == transaction_id,
            Transaction.org_id == current_user.org_id,
        )
    )
    tx = result.scalar_one_or_none()
    if tx is None:
        raise HTTPException(status_code=404, detail="Transaction not found")

    # Revert balance impact (with row lock)
    acct = await _get_account_for_update(db, tx.account_id, current_user.org_id)
    if tx.type == TransactionType.INCOME:
        acct.balance -= tx.amount
    else:
        acct.balance += tx.amount

    await db.delete(tx)
    await db.commit()
