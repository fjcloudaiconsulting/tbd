from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models.subscription import Plan, Subscription
from app.models.user import User
from app.schemas.subscription import PlanCreate, PlanResponse, PlanUpdate

router = APIRouter(prefix="/api/v1/plans", tags=["plans"])


def _require_superadmin(user: User) -> None:
    if not user.is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Superadmin access required",
        )


@router.get("", response_model=list[PlanResponse])
async def list_plans(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all plans. Any authenticated user can view (for plan selection UI)."""
    result = await db.execute(
        select(Plan).where(Plan.is_active == True).order_by(Plan.sort_order)
    )
    return result.scalars().all()


@router.get("/all", response_model=list[PlanResponse])
async def list_all_plans(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all plans including inactive. Superadmin only."""
    _require_superadmin(current_user)
    result = await db.execute(select(Plan).order_by(Plan.sort_order))
    return result.scalars().all()


@router.get("/{plan_id}")
async def get_plan(
    plan_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a single plan with org count. Superadmin only."""
    _require_superadmin(current_user)
    result = await db.execute(select(Plan).where(Plan.id == plan_id))
    plan = result.scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    org_count = await db.scalar(
        select(func.count()).select_from(Subscription).where(
            Subscription.plan_id == plan_id
        )
    )

    return {
        **PlanResponse.model_validate(plan).model_dump(),
        "org_count": org_count,
    }


@router.post("", response_model=PlanResponse, status_code=201)
async def create_plan(
    body: PlanCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new plan. Superadmin only."""
    _require_superadmin(current_user)

    existing = await db.execute(select(Plan).where(Plan.slug == body.slug))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Plan slug already exists")

    plan = Plan(**body.model_dump())
    db.add(plan)
    await db.commit()
    await db.refresh(plan)
    return plan


@router.put("/{plan_id}", response_model=PlanResponse)
async def update_plan(
    plan_id: int,
    body: PlanUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a plan. Superadmin only."""
    _require_superadmin(current_user)

    result = await db.execute(select(Plan).where(Plan.id == plan_id))
    plan = result.scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(plan, field, value)

    await db.commit()
    await db.refresh(plan)
    return plan


@router.delete("/{plan_id}", status_code=204)
async def delete_plan(
    plan_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete (deactivate) a plan. Superadmin only. Cannot delete if orgs are on it."""
    _require_superadmin(current_user)

    result = await db.execute(select(Plan).where(Plan.id == plan_id))
    plan = result.scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    org_count = await db.scalar(
        select(func.count()).select_from(Subscription).where(
            Subscription.plan_id == plan_id
        )
    )
    if org_count > 0:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete plan — {org_count} organization(s) are currently on it",
        )

    plan.is_active = False
    await db.commit()
