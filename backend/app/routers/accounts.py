from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.deps import get_current_user
from app.models.account import Account, AccountType
from app.models.transaction import Transaction
from app.models.user import User
from app.schemas.account import AccountCreate, AccountResponse, AccountUpdate, ReconcileResponse
from app.services.transaction_service import assert_no_dependents, reconcile_account

router = APIRouter(prefix="/api/v1/accounts", tags=["accounts"])


def _to_response(account: Account) -> AccountResponse:
    return AccountResponse(
        id=account.id,
        name=account.name,
        account_type_id=account.account_type_id,
        account_type_name=account.account_type.name if account.account_type else "",
        account_type_slug=account.account_type.slug if account.account_type else None,
        balance=account.balance,
        currency=account.currency,
        is_active=account.is_active,
        close_day=account.close_day,
        is_default=account.is_default,
    )


@router.get("", response_model=list[AccountResponse])
async def list_accounts(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Account)
        .options(selectinload(Account.account_type))
        .where(Account.org_id == current_user.org_id)
        .order_by(Account.name)
    )
    return [_to_response(a) for a in result.scalars().all()]


@router.post("", response_model=AccountResponse, status_code=201)
async def create_account(
    body: AccountCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    at_result = await db.execute(
        select(AccountType).where(
            AccountType.id == body.account_type_id,
            AccountType.org_id == current_user.org_id,
        )
    )
    if at_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=400, detail="Invalid account type")

    account = Account(
        org_id=current_user.org_id,
        account_type_id=body.account_type_id,
        name=body.name,
        balance=body.balance,
        currency=body.currency,
        close_day=body.close_day,
    )
    db.add(account)
    await db.commit()

    result = await db.execute(
        select(Account)
        .options(selectinload(Account.account_type))
        .where(Account.id == account.id)
    )
    return _to_response(result.scalar_one())


@router.get("/{account_id}", response_model=AccountResponse)
async def get_account(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Account)
        .options(selectinload(Account.account_type))
        .where(Account.id == account_id, Account.org_id == current_user.org_id)
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return _to_response(account)


@router.put("/{account_id}", response_model=AccountResponse)
async def update_account(
    account_id: int,
    body: AccountUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Account)
        .options(selectinload(Account.account_type))
        .where(Account.id == account_id, Account.org_id == current_user.org_id)
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    if body.name is not None:
        account.name = body.name
    if body.account_type_id is not None:
        at_result = await db.execute(
            select(AccountType).where(
                AccountType.id == body.account_type_id,
                AccountType.org_id == current_user.org_id,
            )
        )
        if at_result.scalar_one_or_none() is None:
            raise HTTPException(status_code=400, detail="Invalid account type")
        account.account_type_id = body.account_type_id
    if body.is_active is not None:
        if body.is_active is False and account.balance != 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot deactivate account with balance {account.balance}. Transfer the balance first.",
            )
        account.is_active = body.is_active
    if "close_day" in body.model_fields_set:
        account.close_day = body.close_day
    if body.is_default is True:
        async with db.begin_nested():
            await db.execute(
                update(Account)
                .where(Account.org_id == current_user.org_id, Account.id != account.id)
                .values(is_default=False)
            )
            account.is_default = True
    elif body.is_default is False:
        account.is_default = False

    await db.commit()

    result = await db.execute(
        select(Account)
        .options(selectinload(Account.account_type))
        .where(Account.id == account.id)
    )
    return _to_response(result.scalar_one())


@router.get("/{account_id}/reconcile", response_model=ReconcileResponse)
async def reconcile(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Account).where(
            Account.id == account_id, Account.org_id == current_user.org_id
        )
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    stored, computed, consistent = await reconcile_account(db, current_user.org_id, account)
    return ReconcileResponse(
        account_id=account_id,
        stored_balance=stored,
        computed_balance=computed,
        is_consistent=consistent,
    )


@router.delete("/{account_id}", status_code=204)
async def delete_account(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Account).where(
            Account.id == account_id, Account.org_id == current_user.org_id
        )
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    if account.balance != 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot delete account with balance {account.balance}. Transfer the balance first.",
        )

    await assert_no_dependents(
        db, Transaction,
        [Transaction.account_id == account.id, Transaction.org_id == current_user.org_id],
        "transaction", "account",
    )

    await db.delete(account)
    await db.commit()
