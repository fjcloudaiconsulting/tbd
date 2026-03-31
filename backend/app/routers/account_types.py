from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models.account import Account, AccountType
from app.models.user import User
from app.schemas.account import AccountTypeCreate, AccountTypeResponse, AccountTypeUpdate
from app.services.transaction_service import assert_no_dependents

router = APIRouter(prefix="/api/v1/account-types", tags=["account-types"])


@router.get("", response_model=list[AccountTypeResponse])
async def list_account_types(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(
            AccountType,
            func.count(Account.id).label("account_count"),
        )
        .outerjoin(
            Account,
            (Account.account_type_id == AccountType.id)
            & (Account.org_id == current_user.org_id),
        )
        .where(AccountType.org_id == current_user.org_id)
        .group_by(AccountType.id)
        .order_by(AccountType.name)
    )
    return [
        AccountTypeResponse(
            id=at.id, name=at.name, slug=at.slug,
            is_system=at.is_system, account_count=count,
        )
        for at, count in result.all()
    ]


@router.post("", response_model=AccountTypeResponse, status_code=201)
async def create_account_type(
    body: AccountTypeCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    at = AccountType(org_id=current_user.org_id, name=body.name)
    db.add(at)
    await db.commit()
    await db.refresh(at)
    return AccountTypeResponse(id=at.id, name=at.name, slug=at.slug, is_system=at.is_system, account_count=0)


@router.put("/{type_id}", response_model=AccountTypeResponse)
async def update_account_type(
    type_id: int,
    body: AccountTypeUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AccountType).where(
            AccountType.id == type_id, AccountType.org_id == current_user.org_id
        )
    )
    at = result.scalar_one_or_none()
    if at is None:
        raise HTTPException(status_code=404, detail="Account type not found")

    if at.is_system:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot rename system account type",
        )

    at.name = body.name
    await db.commit()
    await db.refresh(at)

    count_result = await db.scalar(
        select(func.count())
        .select_from(Account)
        .where(Account.account_type_id == at.id, Account.org_id == current_user.org_id)
    )
    return AccountTypeResponse(
        id=at.id, name=at.name, slug=at.slug,
        is_system=at.is_system, account_count=count_result or 0,
    )


@router.delete("/{type_id}", status_code=204)
async def delete_account_type(
    type_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AccountType).where(
            AccountType.id == type_id, AccountType.org_id == current_user.org_id
        )
    )
    at = result.scalar_one_or_none()
    if at is None:
        raise HTTPException(status_code=404, detail="Account type not found")

    if at.is_system:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete system account type",
        )

    await assert_no_dependents(
        db, Account,
        [Account.account_type_id == at.id, Account.org_id == current_user.org_id],
        "account", "type",
    )

    await db.delete(at)
    await db.commit()
