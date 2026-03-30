from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.deps import get_current_user
from app.models.account import Account, AccountType
from app.models.user import User
from app.schemas.account import AccountCreate, AccountResponse, AccountUpdate

router = APIRouter(prefix="/api/v1/accounts", tags=["accounts"])


def _to_response(account: Account) -> AccountResponse:
    return AccountResponse(
        id=account.id,
        name=account.name,
        account_type_id=account.account_type_id,
        account_type_name=account.account_type.name if account.account_type else "",
        balance=account.balance,
        currency=account.currency,
        is_active=account.is_active,
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
    # Verify account type belongs to same org
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
        account.is_active = body.is_active

    await db.commit()

    result = await db.execute(
        select(Account)
        .options(selectinload(Account.account_type))
        .where(Account.id == account.id)
    )
    return _to_response(result.scalar_one())


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

    await db.delete(account)
    await db.commit()
