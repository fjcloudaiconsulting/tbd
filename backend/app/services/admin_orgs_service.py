"""Admin org-management service (L4.3).

Three concerns live here, kept out of the router so they're testable
in isolation:

- `list_orgs` / `get_org_detail` — read shapes for the admin UI.
- `update_subscription` — superadmin-only subscription override.
- `delete_org_cascade` — removes the org and every row tied to it,
  in a dependency-safe order. The category self-FK is broken first
  by nulling `parent_id`, otherwise MySQL's strict FK refuses the
  bulk DELETE.
"""

from __future__ import annotations

import datetime
from typing import Optional

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountType
from app.models.billing import BillingPeriod
from app.models.budget import Budget
from app.models.category import Category
from app.models.forecast_plan import ForecastPlan, ForecastPlanItem
from app.models.invitation import Invitation
from app.models.recurring import RecurringTransaction
from app.models.settings import OrgSetting
from app.models.subscription import Plan, Subscription, SubscriptionStatus
from app.models.transaction import Transaction
from app.models.user import Organization, User
from app.services.exceptions import ConflictError, NotFoundError, ValidationError


def _serialize_subscription(sub: Optional[Subscription], plan: Optional[Plan]) -> dict:
    if sub is None:
        return {}
    return {
        "status": sub.status.value,
        "plan_id": sub.plan_id,
        "plan_slug": plan.slug if plan else None,
        "trial_start": sub.trial_start.isoformat() if sub.trial_start else None,
        "trial_end": sub.trial_end.isoformat() if sub.trial_end else None,
        "current_period_start": sub.current_period_start.isoformat() if sub.current_period_start else None,
        "current_period_end": sub.current_period_end.isoformat() if sub.current_period_end else None,
        "created_at": sub.created_at.isoformat() if sub.created_at else None,
        "updated_at": sub.updated_at.isoformat() if sub.updated_at else None,
    }


async def list_orgs(
    db: AsyncSession,
    *,
    q: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Paginated org list for the admin table.

    Served by a single SELECT with LEFT JOIN to subscriptions/plans
    plus correlated user-count subqueries — bounded query cost
    regardless of page size. `last_user_created_at` is a soft proxy
    ("Newest member") for activity until L4.7 audit log lands.
    """
    user_count_sq = (
        select(func.count())
        .select_from(User)
        .where(User.org_id == Organization.id)
        .correlate(Organization)
        .scalar_subquery()
    )
    active_user_count_sq = (
        select(func.count())
        .select_from(User)
        .where(User.org_id == Organization.id, User.is_active.is_(True))
        .correlate(Organization)
        .scalar_subquery()
    )
    newest_member_sq = (
        select(func.max(User.created_at))
        .where(User.org_id == Organization.id)
        .correlate(Organization)
        .scalar_subquery()
    )

    stmt = (
        select(
            Organization.id,
            Organization.name,
            Organization.created_at,
            Subscription.status,
            Subscription.trial_end,
            Plan.slug,
            user_count_sq.label("user_count"),
            active_user_count_sq.label("active_user_count"),
            newest_member_sq.label("last_user_created_at"),
        )
        .select_from(Organization)
        .outerjoin(Subscription, Subscription.org_id == Organization.id)
        .outerjoin(Plan, Plan.id == Subscription.plan_id)
    )
    if q:
        stmt = stmt.where(Organization.name.ilike(f"%{q}%"))

    total_stmt = select(func.count()).select_from(Organization)
    if q:
        total_stmt = total_stmt.where(Organization.name.ilike(f"%{q}%"))
    total = (await db.scalar(total_stmt)) or 0

    rows = (
        await db.execute(
            stmt.order_by(Organization.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    items = [
        {
            "id": row.id,
            "name": row.name,
            "plan_slug": row.slug,
            "subscription_status": row.status.value if row.status else None,
            "trial_end": row.trial_end.isoformat() if row.trial_end else None,
            "user_count": row.user_count or 0,
            "active_user_count": row.active_user_count or 0,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "last_user_created_at": (
                row.last_user_created_at.isoformat()
                if row.last_user_created_at else None
            ),
        }
        for row in rows
    ]

    return {"items": items, "total": total, "limit": limit, "offset": offset}


async def get_org_detail(db: AsyncSession, *, org_id: int) -> dict:
    org = (
        await db.execute(select(Organization).where(Organization.id == org_id))
    ).scalar_one_or_none()
    if org is None:
        raise NotFoundError("Organization")

    sub = (
        await db.execute(select(Subscription).where(Subscription.org_id == org_id))
    ).scalar_one_or_none()
    plan = None
    if sub is not None:
        plan = (
            await db.execute(select(Plan).where(Plan.id == sub.plan_id))
        ).scalar_one_or_none()

    members = (
        await db.execute(
            select(User).where(User.org_id == org_id).order_by(User.username)
        )
    ).scalars().all()

    counts = {
        "transactions": await db.scalar(
            select(func.count()).select_from(Transaction).where(Transaction.org_id == org_id)
        ) or 0,
        "accounts": await db.scalar(
            select(func.count()).select_from(Account).where(Account.org_id == org_id)
        ) or 0,
        "budgets": await db.scalar(
            select(func.count()).select_from(Budget).where(Budget.org_id == org_id)
        ) or 0,
        "forecast_plans": await db.scalar(
            select(func.count()).select_from(ForecastPlan).where(ForecastPlan.org_id == org_id)
        ) or 0,
    }

    return {
        "id": org.id,
        "name": org.name,
        "billing_cycle_day": org.billing_cycle_day,
        "created_at": org.created_at.isoformat() if org.created_at else None,
        "subscription": _serialize_subscription(sub, plan),
        "members": [
            {
                "id": u.id, "username": u.username, "email": u.email,
                "role": u.role.value, "is_active": u.is_active,
                "email_verified": u.email_verified,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            }
            for u in members
        ],
        "counts": counts,
    }


async def update_subscription(
    db: AsyncSession,
    *,
    org_id: int,
    plan_id: Optional[int] = None,
    status: Optional[SubscriptionStatus] = None,
    trial_end: Optional[datetime.date] = None,
    current_period_end: Optional[datetime.date] = None,
) -> tuple[dict, dict]:
    """Apply provided fields to the org's subscription. Returns
    `(before, after)` dicts containing ONLY the fields that changed —
    the caller logs this for audit. Raises NotFoundError if the org
    has no subscription, ValidationError if `plan_id` doesn't exist.
    """
    sub = (
        await db.execute(select(Subscription).where(Subscription.org_id == org_id))
    ).scalar_one_or_none()
    if sub is None:
        raise NotFoundError("Subscription")

    if plan_id is not None:
        plan = (
            await db.execute(select(Plan).where(Plan.id == plan_id))
        ).scalar_one_or_none()
        if plan is None:
            raise ValidationError("Unknown plan_id")

    before: dict = {}
    after: dict = {}

    def _track(field: str, new_value, current):
        before[field] = (
            current.isoformat() if hasattr(current, "isoformat") else
            (current.value if hasattr(current, "value") else current)
        )
        after[field] = (
            new_value.isoformat() if hasattr(new_value, "isoformat") else
            (new_value.value if hasattr(new_value, "value") else new_value)
        )

    if plan_id is not None and plan_id != sub.plan_id:
        _track("plan_id", plan_id, sub.plan_id)
        sub.plan_id = plan_id
    if status is not None and status != sub.status:
        _track("status", status, sub.status)
        sub.status = status
    if trial_end is not None and trial_end != sub.trial_end:
        _track("trial_end", trial_end, sub.trial_end)
        sub.trial_end = trial_end
    if current_period_end is not None and current_period_end != sub.current_period_end:
        _track("current_period_end", current_period_end, sub.current_period_end)
        sub.current_period_end = current_period_end

    await db.flush()
    return before, after


async def delete_org_cascade(
    db: AsyncSession, *, org_id: int
) -> dict[str, int]:
    """Delete the org and every row that references it.

    Returns a dict of `{table_name: row_count_deleted}` so the caller
    can log it for audit. Caller commits.
    """
    org = (
        await db.execute(select(Organization).where(Organization.id == org_id))
    ).scalar_one_or_none()
    if org is None:
        raise NotFoundError("Organization")

    counts: dict[str, int] = {}

    # Order matters: delete children before parents.
    # transactions reference accounts, categories, recurring → first.
    counts["transactions"] = (
        await db.execute(delete(Transaction).where(Transaction.org_id == org_id))
    ).rowcount or 0

    counts["forecast_plan_items"] = (
        await db.execute(
            delete(ForecastPlanItem).where(ForecastPlanItem.org_id == org_id)
        )
    ).rowcount or 0

    counts["budgets"] = (
        await db.execute(delete(Budget).where(Budget.org_id == org_id))
    ).rowcount or 0

    counts["invitations"] = (
        await db.execute(delete(Invitation).where(Invitation.org_id == org_id))
    ).rowcount or 0

    counts["recurring_transactions"] = (
        await db.execute(
            delete(RecurringTransaction).where(RecurringTransaction.org_id == org_id)
        )
    ).rowcount or 0

    counts["forecast_plans"] = (
        await db.execute(delete(ForecastPlan).where(ForecastPlan.org_id == org_id))
    ).rowcount or 0

    counts["billing_periods"] = (
        await db.execute(delete(BillingPeriod).where(BillingPeriod.org_id == org_id))
    ).rowcount or 0

    counts["accounts"] = (
        await db.execute(delete(Account).where(Account.org_id == org_id))
    ).rowcount or 0

    counts["account_types"] = (
        await db.execute(delete(AccountType).where(AccountType.org_id == org_id))
    ).rowcount or 0

    # Categories self-reference via parent_id. Break the link before
    # the bulk DELETE so MySQL's strict FK doesn't refuse.
    await db.execute(
        update(Category).where(Category.org_id == org_id).values(parent_id=None)
    )
    counts["categories"] = (
        await db.execute(delete(Category).where(Category.org_id == org_id))
    ).rowcount or 0

    counts["settings"] = (
        await db.execute(delete(OrgSetting).where(OrgSetting.org_id == org_id))
    ).rowcount or 0

    counts["users"] = (
        await db.execute(delete(User).where(User.org_id == org_id))
    ).rowcount or 0

    counts["subscriptions"] = (
        await db.execute(delete(Subscription).where(Subscription.org_id == org_id))
    ).rowcount or 0

    counts["organizations"] = (
        await db.execute(delete(Organization).where(Organization.id == org_id))
    ).rowcount or 0

    return counts
