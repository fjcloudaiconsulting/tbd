"""Subscription service — trial lifecycle, plan changes, feature enforcement."""

import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.subscription import (
    BillingInterval,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.models.user import User
from app.services.exceptions import NotFoundError, ValidationError


async def get_default_plan(db: AsyncSession) -> Plan:
    """Get the plan configured as the default for new orgs."""
    result = await db.execute(
        select(Plan).where(Plan.slug == settings.default_plan_slug, Plan.is_active == True)
    )
    plan = result.scalar_one_or_none()
    if plan is None:
        # Fallback to any active plan
        result = await db.execute(
            select(Plan).where(Plan.is_active == True).order_by(Plan.sort_order).limit(1)
        )
        plan = result.scalar_one_or_none()
    if plan is None:
        raise RuntimeError("No active plans configured — seed the database")
    return plan


async def create_trial(db: AsyncSession, org_id: int) -> Subscription:
    """Create a trial subscription for a new org."""
    plan = await get_default_plan(db)
    today = datetime.date.today()
    trial_end = today + datetime.timedelta(days=settings.trial_duration_days)

    subscription = Subscription(
        org_id=org_id,
        plan_id=plan.id,
        status=SubscriptionStatus.TRIALING,
        trial_start=today,
        trial_end=trial_end,
    )
    db.add(subscription)
    await db.flush()
    return subscription


async def get_subscription(db: AsyncSession, org_id: int) -> Subscription | None:
    """Get the subscription for an org, or None if not found."""
    result = await db.execute(
        select(Subscription).where(Subscription.org_id == org_id)
    )
    return result.scalar_one_or_none()


async def get_subscription_with_plan(
    db: AsyncSession, org_id: int
) -> tuple[Subscription, Plan] | None:
    """Get subscription + plan for an org."""
    result = await db.execute(
        select(Subscription, Plan)
        .join(Plan, Subscription.plan_id == Plan.id)
        .where(Subscription.org_id == org_id)
    )
    row = result.first()
    if row is None:
        return None
    return row[0], row[1]


async def check_trial_expiry(db: AsyncSession, org_id: int) -> Subscription | None:
    """Check if trial has expired and downgrade if needed. Returns updated subscription."""
    sub = await get_subscription(db, org_id)
    if sub is None:
        return None

    if sub.status != SubscriptionStatus.TRIALING:
        return sub

    if sub.trial_end and sub.trial_end < datetime.date.today():
        # Trial expired — downgrade to free plan
        free_plan = await db.execute(
            select(Plan).where(Plan.slug == "free", Plan.is_active == True)
        )
        free = free_plan.scalar_one_or_none()
        if free:
            sub.plan_id = free.id
        sub.status = SubscriptionStatus.ACTIVE
        sub.trial_start = None
        sub.trial_end = None
        await db.commit()
        await db.refresh(sub)

    return sub


async def change_plan(
    db: AsyncSession, org_id: int, plan_slug: str, billing_interval: str
) -> Subscription:
    """Change an org's plan. Instant switch."""
    result = await db.execute(
        select(Plan).where(Plan.slug == plan_slug, Plan.is_active == True)
    )
    new_plan = result.scalar_one_or_none()
    if new_plan is None:
        raise NotFoundError("Plan")

    sub = await get_subscription(db, org_id)
    if sub is None:
        raise NotFoundError("Subscription")

    # Validate billing interval
    try:
        interval = BillingInterval(billing_interval)
    except ValueError:
        raise ValidationError("Invalid billing interval — use 'monthly' or 'yearly'")

    sub.plan_id = new_plan.id
    sub.billing_interval = interval

    # If upgrading from trial, convert to active
    if sub.status == SubscriptionStatus.TRIALING:
        sub.status = SubscriptionStatus.ACTIVE
        sub.trial_start = None
        sub.trial_end = None
        # In mock mode, set a 30-day period
        today = datetime.date.today()
        sub.current_period_start = today
        if interval == BillingInterval.MONTHLY:
            sub.current_period_end = today + datetime.timedelta(days=30)
        else:
            sub.current_period_end = today + datetime.timedelta(days=365)

    # If changing plan on active subscription, keep current period
    # (real billing would prorate — mock just swaps)

    await db.commit()
    await db.refresh(sub)
    return sub


async def cancel_subscription(db: AsyncSession, org_id: int) -> Subscription:
    """Cancel subscription. Access continues until current period ends."""
    sub = await get_subscription(db, org_id)
    if sub is None:
        raise NotFoundError("Subscription")

    sub.status = SubscriptionStatus.CANCELED
    await db.commit()
    await db.refresh(sub)
    return sub


async def enforce_user_limit(db: AsyncSession, org_id: int) -> None:
    """Check if the org can add another user. Raises ValidationError if at limit."""
    pair = await get_subscription_with_plan(db, org_id)
    if pair is None:
        return  # No subscription = no limit (shouldn't happen)

    _, plan = pair
    if plan.max_users is None:
        return  # Unlimited

    user_count = await db.scalar(
        select(func.count()).select_from(User).where(
            User.org_id == org_id, User.is_active == True
        )
    )
    if user_count >= plan.max_users:
        raise ValidationError(
            f"Your plan ({plan.name}) allows a maximum of {plan.max_users} user(s). "
            "Upgrade your plan to add more users."
        )
