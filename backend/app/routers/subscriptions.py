from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models.user import Role, User
from app.rate_limit import get_client_ip
from app.schemas.subscription import ChangePlanRequest, PlanResponse, SubscriptionResponse
from app.services import audit_service, subscription_service
from app import logs

router = APIRouter(prefix="/api/v1/subscriptions", tags=["subscriptions"])


def _request_id() -> str | None:
    """Pull the per-request id bound by RequestContextMiddleware."""
    return logs.CONTEXT.get({}).get("request_id")


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
        # PlanResponse canonicalizes features for the wire shape.
        "plan": PlanResponse.model_validate(plan).model_dump(),
        "status": sub.status.value,
        "billing_interval": sub.billing_interval.value,
        "trial_start": sub.trial_start.isoformat() if sub.trial_start else None,
        "trial_end": sub.trial_end.isoformat() if sub.trial_end else None,
        "current_period_start": sub.current_period_start.isoformat() if sub.current_period_start else None,
        "current_period_end": sub.current_period_end.isoformat() if sub.current_period_end else None,
    }


def _snapshot(pair) -> dict:
    """Scalars only, read before the service commits."""
    if pair is None:
        return {"plan_slug": None, "status": None, "billing_interval": None}
    sub, plan = pair
    return {
        "plan_slug": plan.slug,
        "status": sub.status.value,
        "billing_interval": sub.billing_interval.value,
    }


async def _audit_subscription_change(
    session_factory, request: Request, actor: tuple[int, str, int], *,
    event_type: str, before: dict, after: dict,
) -> None:
    actor_id, actor_email, org_id = actor
    await audit_service.record_audit_event(
        session_factory,
        event_type=event_type,
        actor_user_id=actor_id,
        actor_email=actor_email,
        target_org_id=org_id,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail={
            "old_plan_slug": before["plan_slug"],
            "new_plan_slug": after["plan_slug"],
            "old_status": before["status"],
            "new_status": after["status"],
            "old_billing_interval": before["billing_interval"],
            "new_billing_interval": after["billing_interval"],
        },
    )


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
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Change the org's plan. Owner only.

    Refused while ``billing_ui_enabled`` is off: no payment is taken yet and
    the only client of this route, ``/settings/billing``, does not call it in
    that state. Operators move an org through the admin console
    (``PUT /api/v1/admin/orgs/{id}/subscription``), which this does not gate.
    """
    _require_owner(current_user)
    if not app_settings.billing_ui_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Plan changes are not available yet",
        )
    # Actor scalars BEFORE the service commits.
    actor = (current_user.id, current_user.email, current_user.org_id)
    before = _snapshot(
        await subscription_service.get_subscription_with_plan(db, current_user.org_id)
    )
    sub = await subscription_service.change_plan(
        db, current_user.org_id, body.plan_slug, body.billing_interval
    )
    pair = await subscription_service.get_subscription_with_plan(db, actor[2])
    sub, plan = pair
    await _audit_subscription_change(
        session_factory, request, actor,
        event_type="org.subscription.plan.changed", before=before, after=_snapshot(pair),
    )
    return _sub_response(sub, plan)


@router.post("/cancel")
async def cancel_subscription(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Cancel the subscription. Owner only. Access continues until period end."""
    _require_owner(current_user)
    actor = (current_user.id, current_user.email, current_user.org_id)
    before = _snapshot(
        await subscription_service.get_subscription_with_plan(db, current_user.org_id)
    )
    sub = await subscription_service.cancel_subscription(db, current_user.org_id)
    pair = await subscription_service.get_subscription_with_plan(db, actor[2])
    sub, plan = pair
    await _audit_subscription_change(
        session_factory, request, actor,
        event_type="org.subscription.canceled", before=before, after=_snapshot(pair),
    )
    return _sub_response(sub, plan)
