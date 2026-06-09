from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import require_permission
from app.database import get_db
from app.deps import get_current_user
from app.models.subscription import Plan, Subscription
from app.models.user import User
from app.schemas.common import ListEnvelope
from app.schemas.subscription import PlanCreate, PlanDuplicateRequest, PlanResponse, PlanUpdate
from app.services.exceptions import ValidationError
from app.services.list_query import resolve_order_by
from app.services.plan_service import canonicalize_features

router = APIRouter(prefix="/api/v1/plans", tags=["plans"])


# Closed sort whitelist for the system/plans admin table. Keys are the
# public sort tokens the frontend sends; values are the columns to order
# by. Limited to the columns the UI actually exposes as headers. Anything
# else is a 400 (see ``list_query.resolve_order_by``).
_PLAN_SORT_COLUMNS = {
    "name": Plan.name,
    "price_monthly": Plan.price_monthly,
    "price_yearly": Plan.price_yearly,
    "max_users": Plan.max_users,
    "retention_days": Plan.retention_days,
    "is_active": Plan.is_active,
}


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


@router.get(
    "/all",
    response_model=ListEnvelope[PlanResponse],
    dependencies=[Depends(require_permission("plans.manage"))],
)
async def list_all_plans(
    sort_by: str | None = Query(default=None),
    sort_dir: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """List all plans including inactive. Requires plans.manage.

    Returns the standard ``ListEnvelope`` so the system/plans table can
    sort and page server-side. Default order is ``sort_order`` asc (the
    operator-curated display order) with ``id`` asc as a stable
    tiebreaker; an explicit ``sort_by`` overrides it against the closed
    whitelist.
    """
    total = (await db.scalar(select(func.count()).select_from(Plan))) or 0

    if sort_by:
        try:
            order_by = resolve_order_by(
                sort_by,
                sort_dir,
                allowed=_PLAN_SORT_COLUMNS,
                default_key="name",
                default_dir="asc",
                tiebreaker=Plan.id.asc(),
            )
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=exc.detail
            ) from exc
    else:
        order_by = [Plan.sort_order.asc(), Plan.id.asc()]

    result = await db.execute(
        select(Plan).order_by(*order_by).limit(limit).offset(offset)
    )
    items = result.scalars().all()
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get(
    "/{plan_id}",
    dependencies=[Depends(require_permission("plans.manage"))],
)
async def get_plan(
    plan_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get a single plan with org count. Requires plans.manage."""
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


@router.post(
    "",
    response_model=PlanResponse,
    status_code=201,
    dependencies=[Depends(require_permission("plans.manage"))],
)
async def create_plan(
    body: PlanCreate,
    db: AsyncSession = Depends(get_db),
):
    """Create a new plan. Requires plans.manage."""
    existing = await db.execute(select(Plan).where(Plan.slug == body.slug))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Plan slug already exists")

    payload = body.model_dump()
    payload["features"] = canonicalize_features(payload.get("features") or {})
    plan = Plan(**payload)
    db.add(plan)
    await db.commit()
    await db.refresh(plan)
    return plan


@router.put(
    "/{plan_id}",
    response_model=PlanResponse,
    dependencies=[Depends(require_permission("plans.manage"))],
)
async def update_plan(
    plan_id: int,
    body: PlanUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update a plan. Requires plans.manage."""
    result = await db.execute(select(Plan).where(Plan.id == plan_id))
    plan = result.scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    update_data = body.model_dump(exclude_unset=True)

    # Prevent deactivation via PUT — must use DELETE which checks org count
    if "is_active" in update_data and not update_data["is_active"]:
        org_count = await db.scalar(
            select(func.count()).select_from(Subscription).where(
                Subscription.plan_id == plan_id
            )
        )
        if org_count > 0:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot deactivate plan — {org_count} organization(s) are currently on it",
            )

    if "features" in update_data:
        update_data["features"] = canonicalize_features(
            update_data["features"] or {}, existing=plan.features
        )

    for field, value in update_data.items():
        setattr(plan, field, value)

    await db.commit()
    await db.refresh(plan)
    return plan


@router.delete(
    "/{plan_id}",
    status_code=204,
    dependencies=[Depends(require_permission("plans.manage"))],
)
async def delete_plan(
    plan_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete (deactivate) a plan. Requires plans.manage. Cannot delete if orgs are on it."""
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


@router.post(
    "/{plan_id}/duplicate",
    response_model=PlanResponse,
    status_code=201,
    dependencies=[Depends(require_permission("plans.manage"))],
)
async def duplicate_plan(
    plan_id: int,
    body: PlanDuplicateRequest,
    db: AsyncSession = Depends(get_db),
):
    """Clone a plan with is_custom=True. Reject 409 on slug conflict."""
    src = (await db.execute(select(Plan).where(Plan.id == plan_id))).scalar_one_or_none()
    if src is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    slug_taken = await db.scalar(select(Plan.id).where(Plan.slug == body.slug))
    if slug_taken is not None:
        raise HTTPException(status_code=409, detail="Plan slug already exists")

    clone = Plan(
        name=body.name,
        slug=body.slug,
        description=src.description,
        is_custom=True,
        is_active=True,
        sort_order=src.sort_order,
        price_monthly=src.price_monthly,
        price_yearly=src.price_yearly,
        max_users=src.max_users,
        retention_days=src.retention_days,
        # Re-canonicalize so the clone always has the full closed-set keys
        # even if the source somehow drifted.
        features=canonicalize_features(src.features or {}),
    )
    db.add(clone)
    await db.commit()
    await db.refresh(clone)
    return clone
