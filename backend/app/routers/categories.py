from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models.category import Category
from app.models.transaction import Transaction
from app.models.user import User
from app.schemas.category import CategoryCreate, CategoryResponse, CategoryUpdate

router = APIRouter(prefix="/api/v1/categories", tags=["categories"])


@router.get("", response_model=list[CategoryResponse])
async def list_categories(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(
            Category,
            func.count(Transaction.id).label("transaction_count"),
        )
        .outerjoin(
            Transaction,
            (Transaction.category_id == Category.id)
            & (Transaction.org_id == current_user.org_id),
        )
        .where(Category.org_id == current_user.org_id)
        .group_by(Category.id)
        .order_by(Category.name)
    )
    return [
        CategoryResponse(id=cat.id, name=cat.name, transaction_count=count)
        for cat, count in result.all()
    ]


@router.post("", response_model=CategoryResponse, status_code=201)
async def create_category(
    body: CategoryCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cat = Category(org_id=current_user.org_id, name=body.name)
    db.add(cat)
    await db.commit()
    await db.refresh(cat)
    return CategoryResponse(id=cat.id, name=cat.name, transaction_count=0)


@router.put("/{category_id}", response_model=CategoryResponse)
async def update_category(
    category_id: int,
    body: CategoryUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Category).where(
            Category.id == category_id, Category.org_id == current_user.org_id
        )
    )
    cat = result.scalar_one_or_none()
    if cat is None:
        raise HTTPException(status_code=404, detail="Category not found")

    cat.name = body.name
    await db.commit()
    await db.refresh(cat)

    count_result = await db.scalar(
        select(func.count())
        .select_from(Transaction)
        .where(
            Transaction.category_id == cat.id,
            Transaction.org_id == current_user.org_id,
        )
    )
    return CategoryResponse(id=cat.id, name=cat.name, transaction_count=count_result or 0)


@router.delete("/{category_id}", status_code=204)
async def delete_category(
    category_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Category).where(
            Category.id == category_id, Category.org_id == current_user.org_id
        )
    )
    cat = result.scalar_one_or_none()
    if cat is None:
        raise HTTPException(status_code=404, detail="Category not found")

    tx_count = await db.scalar(
        select(func.count())
        .select_from(Transaction)
        .where(
            Transaction.category_id == cat.id,
            Transaction.org_id == current_user.org_id,
        )
    )
    if tx_count and tx_count > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot delete: {tx_count} transaction(s) use this category",
        )

    await db.delete(cat)
    await db.commit()
