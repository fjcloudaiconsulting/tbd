from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.database import get_db
from app.deps import get_current_user
from app.models.category import Category, CategoryType
from app.models.transaction import Transaction
from app.models.user import User
from app.schemas.category import CategoryCreate, CategoryResponse, CategoryUpdate
from app.services.transaction_service import assert_no_dependents

router = APIRouter(prefix="/api/v1/categories", tags=["categories"])

Parent = aliased(Category)


def _to_response(cat: Category, parent_name: str | None, tx_count: int) -> CategoryResponse:
    return CategoryResponse(
        id=cat.id,
        name=cat.name,
        type=cat.type.value,
        parent_id=cat.parent_id,
        parent_name=parent_name,
        description=cat.description,
        slug=cat.slug,
        is_system=cat.is_system,
        transaction_count=tx_count,
    )


@router.get("", response_model=list[CategoryResponse])
async def list_categories(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(
            Category,
            Parent.name.label("parent_name"),
            func.count(Transaction.id).label("transaction_count"),
        )
        .outerjoin(Parent, Category.parent_id == Parent.id)
        .outerjoin(
            Transaction,
            (Transaction.category_id == Category.id)
            & (Transaction.org_id == current_user.org_id),
        )
        .where(Category.org_id == current_user.org_id)
        .group_by(Category.id, Parent.name)
        .order_by(Category.parent_id.is_(None).desc(), Parent.name, Category.name)
    )
    return [
        _to_response(cat, parent_name, count)
        for cat, parent_name, count in result.all()
    ]


@router.post("", response_model=CategoryResponse, status_code=201)
async def create_category(
    body: CategoryCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.parent_id is not None:
        parent_row = await db.execute(
            select(Category).where(
                Category.id == body.parent_id, Category.org_id == current_user.org_id
            )
        )
        parent_cat = parent_row.scalar_one_or_none()
        if parent_cat is None:
            raise HTTPException(status_code=400, detail="Invalid parent category")
        if parent_cat.parent_id is not None:
            raise HTTPException(status_code=400, detail="Cannot nest more than two levels")

    cat = Category(
        org_id=current_user.org_id,
        name=body.name,
        type=CategoryType(body.type),
        parent_id=body.parent_id,
        description=body.description,
    )
    db.add(cat)
    await db.commit()
    await db.refresh(cat)

    parent_name = None
    if cat.parent_id:
        parent_name = await db.scalar(
            select(Category.name).where(
                Category.id == cat.parent_id, Category.org_id == current_user.org_id
            )
        )

    return _to_response(cat, parent_name, 0)


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

    if body.name is not None:
        cat.name = body.name
    if body.type is not None:
        cat.type = CategoryType(body.type)
    if body.description is not None:
        cat.description = body.description
    await db.commit()
    await db.refresh(cat)

    parent_name = None
    if cat.parent_id:
        parent_name = await db.scalar(
            select(Category.name).where(
                Category.id == cat.parent_id, Category.org_id == current_user.org_id
            )
        )

    count_result = await db.scalar(
        select(func.count())
        .select_from(Transaction)
        .where(
            Transaction.category_id == cat.id,
            Transaction.org_id == current_user.org_id,
        )
    )
    return _to_response(cat, parent_name, count_result or 0)


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

    # Check for child categories
    await assert_no_dependents(
        db, Category,
        [Category.parent_id == cat.id, Category.org_id == current_user.org_id],
        "subcategory", "category",
    )

    # Check for transactions
    await assert_no_dependents(
        db, Transaction,
        [Transaction.category_id == cat.id, Transaction.org_id == current_user.org_id],
        "transaction", "category",
    )

    await db.delete(cat)
    await db.commit()
