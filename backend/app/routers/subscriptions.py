from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models.user import Role, User
from app.schemas.subscription import ChangePlanRequest, SubscriptionResponse
from app.services import subscription_service

router = APIRouter(prefix="/api/v1/subscriptions", tags=["subscriptions"])


def _require_owner(user: User) -> None:
    if user.role != Role.OWNER and not user.is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the organization owner can manage billing",
        )


def _sub_response(sub, plan) -> dict:
    return {
        "id": sub.id,
        "org_id": sub.org_id,
        "plan": {
            "id": plan.id,
            "name": plan.name,
            "slug": plan.slug,
            "description": plan.description,
            "is_custom": plan.is_custom,
            "is_active": plan.is_active,
            "sort_order": plan.sort_order,
            "price_monthly": float(plan.price_monthly),
            "price_yearly": float(plan.price_yearly),
            "max_users": plan.max_users,
            "retention_days": plan.retention_days,
            "ai_budget_enabled": plan.ai_budget_enabled,
            "ai_forecast_enabled": plan.ai_forecast_enabled,
            "ai_smart_plan_enabled": plan.ai_smart_plan_enabled,
        },
        "status": sub.status.value,
        "billing_interval": sub.billing_interval.value,
        "trial_start": sub.trial_start.isoformat() if sub.trial_start else None,
        "trial_end": sub.trial_end.isoformat() if sub.trial_end else None,
        "current_period_start": sub.current_period_start.isoformat() if sub.current_period_start else None,
        "current_period_end": sub.current_period_end.isoformat() if sub.current_period_end else None,
    }


@router.get("")
async def get_subscription(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the current org's subscription. Any authenticated user can view."""
    await subscription_service.check_trial_expiry(db, current_user.org_id)
    pair = await subscription_service.get_subscription_with_plan(db, current_user.org_id)
    if pair is None:
        raise HTTPException(status_code=404, detail="No subscription found")
    sub, plan = pair
    return _sub_response(sub, plan)


@router.put("/plan")
async def change_plan(
    body: ChangePlanRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change the org's plan. Owner only."""
    _require_owner(current_user)
    sub = await subscription_service.change_plan(
        db, current_user.org_id, body.plan_slug, body.billing_interval
    )
    pair = await subscription_service.get_subscription_with_plan(db, current_user.org_id)
    sub, plan = pair
    return _sub_response(sub, plan)


@router.post("/cancel")
async def cancel_subscription(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Cancel the subscription. Owner only. Access continues until period end."""
    _require_owner(current_user)
    sub = await subscription_service.cancel_subscription(db, current_user.org_id)
    pair = await subscription_service.get_subscription_with_plan(db, current_user.org_id)
    sub, plan = pair
    return _sub_response(sub, plan)
