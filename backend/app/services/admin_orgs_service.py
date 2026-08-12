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

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.ai_usage_ledger import AIUsageLedger
from app.models.dashboard import DashboardLayout
from app.models.org_ai_credential import OrgAICredential
from app.models.report import Report
from app.models.budget import Budget
from app.models.feature_override import OrgFeatureOverride
from app.models.forecast_plan import ForecastPlan
from app.models.invitation import Invitation
from app.models.settings import OrgSetting
from app.models.subscription import Plan, Subscription, SubscriptionStatus
from app.models.transaction import Transaction
from app.models.user import Organization, User
from app.services.exceptions import ConflictError, NotFoundError, ValidationError

from app.services.list_query import resolve_order_by
from app.services.org_data_service import wipe_org_data
# Machine-readable refusal code so the router can branch without parsing
# English — the same convention as invitation_service / admin_users_service.
# ⚠ Do NOT match on the message text: admin_orgs.py already does that for a
# different ConflictError and it is a defect waiting to fire (TBD-374).
CODE_ORG_HOLDS_SUPERADMIN = "org_holds_platform_superadmin"



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
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Paginated org list for the admin table.

    Served by a single SELECT with LEFT JOIN to subscriptions/plans
    plus correlated user-count subqueries — bounded query cost
    regardless of page size. `last_user_created_at` is a soft proxy
    ("Newest member") for activity until L4.7 audit log lands.

    ``sort_by`` is resolved against a closed whitelist (see below); an
    unknown key raises ``ValidationError`` (router → 400). When omitted,
    defaults to ``created_at`` desc. ``Organization.id`` desc is appended
    as a stable tiebreaker so pagination is deterministic.
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

    # Closed whitelist of sortable columns. Keys are the public sort
    # tokens the frontend sends; values are the column/expression to
    # order by. Subquery aggregates (user_count / last member) order on
    # the bare subquery expression — MySQL and SQLite both accept a
    # scalar subquery in ORDER BY. Anything not here is a 400 (see
    # ``list_query.resolve_order_by``).
    sortable = {
        "name": Organization.name,
        "created_at": Organization.created_at,
        "plan_slug": Plan.slug,
        "subscription_status": Subscription.status,
        "user_count": user_count_sq,
        "active_user_count": active_user_count_sq,
        "last_user_created_at": newest_member_sq,
    }

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

    order_by = resolve_order_by(
        sort_by,
        sort_dir,
        allowed=sortable,
        default_key="created_at",
        default_dir="desc",
        tiebreaker=Organization.id.desc(),
    )

    rows = (
        await db.execute(
            stmt.order_by(*order_by)
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

    # ⚠ Refuse when the org houses a platform superadmin (TBD-342, folded from
    # TBD-373). This guard is only REACHABLE because this function now works:
    # before the RESTRICT repair below it raised on a foreign key first, so the
    # unguarded hard-delete was masked in practice. Repairing deletion without
    # this would convert a fail-safe error into a working, unguarded,
    # destructive path.
    #
    # The harm is NOT privilege escalation — that reading was refuted by
    # measurement: the caller refuses to delete its own org, org_id is NOT
    # NULL, and orgs.manage is reachable only via the is_superadmin
    # short-circuit, so the actor's own row is structurally outside the delete
    # set and count(is_superadmin) >= 1 always. The harm is destroying platform
    # admins with no operator signal, and anonymising their entire audit
    # history — audit_events.actor_user_id is ON DELETE SET NULL, so those rows
    # survive only through the actor_email snapshot.
    #
    # ⚠ The own-org invariant lives in the ROUTER, not here. This guard is what
    # makes the service safe for a SECOND caller that lacks it.
    superadmins = (
        await db.scalar(
            select(func.count())
            .select_from(User)
            # ⚠ NO is_active filter, deliberately. A deactivated superadmin
            # row still HOLDS the platform flag, and hard-deleting it is the
            # same irreversible harm the guard exists to prevent — the account
            # is gone and audit_events.actor_user_id nulls out, anonymising
            # their history. Soft-deleted is recoverable (TBD-377); deleted is
            # not. Pinned by test_inactive_superadmin_also_blocks_org_delete.
            .where(User.org_id == org_id, User.is_superadmin.is_(True))
        )
    ) or 0
    if superadmins:
        raise ConflictError(
            f"This organization holds {superadmins} platform administrator "
            "account(s). Remove or move them before deleting the organization.",
            code=CODE_ORG_HOLDS_SUPERADMIN,
        )

    # Wipe org-scoped data tables via the shared helper. Single source
    # of truth for the FK-safe wipe order — also used by the tenant
    # reset path in org_data_service.reset_org_data.
    counts = await wipe_org_data(db, org_id=org_id)

    # Org-shell tables (only the admin path deletes these):

    # ── RESTRICT foreign keys (TBD-342) ────────────────────────────────────
    # Every RESTRICT foreign key must be cleared before its parent row.
    # dashboard_layouts.owner_user_id and reports.owner_user_id are RESTRICT
    # against USERS (their org_id FKs are CASCADE, which fires too late — the
    # org row goes after the users); org_ai_credentials.org_id and
    # ai_usage_ledger.org_id are RESTRICT against the ORGANIZATION.
    # None had a service-layer delete here, and dashboard.py::_get_or_create
    # gives every user who opens the dashboard a layout row, so this function
    # raised MySQL 1451 for essentially every real org.
    counts["dashboard_layouts"] = (
        await db.execute(
            delete(DashboardLayout).where(DashboardLayout.org_id == org_id)
        )
    ).rowcount or 0
    # ⚠ reports.owner_user_id is ALSO ondelete="RESTRICT" (models/report.py).
    # admin_users_service.delete_user handles it for the SINGLE-USER path, which
    # made the column look covered — it is not covered here. reports.org_id
    # being CASCADE does not save us: that fires when `organizations` is
    # deleted, which happens AFTER the users delete below, so the user delete
    # raises first. report_versions ride their own CASCADE off reports.id.
    counts["reports"] = (
        await db.execute(delete(Report).where(Report.org_id == org_id))
    ).rowcount or 0
    # Ledger BEFORE credentials: ai_usage_ledger.credential_id is
    # ON DELETE SET NULL, so deleting credentials first makes MySQL run a full
    # UPDATE pass over ledger rows we are about to delete anyway.
    counts["ai_usage_ledger"] = (
        await db.execute(delete(AIUsageLedger).where(AIUsageLedger.org_id == org_id))
    ).rowcount or 0
    # Note: org_ai_default_routing / org_ai_feature_routing reference
    # credentials ON DELETE CASCADE, so those rows die with this statement and
    # are not counted separately.
    counts["org_ai_credentials"] = (
        await db.execute(
            delete(OrgAICredential).where(OrgAICredential.org_id == org_id)
        )
    ).rowcount or 0

    counts["invitations"] = (
        await db.execute(delete(Invitation).where(Invitation.org_id == org_id))
    ).rowcount or 0

    counts["settings"] = (
        await db.execute(delete(OrgSetting).where(OrgSetting.org_id == org_id))
    ).rowcount or 0

    # L4.11: per-org feature overrides. set_by FKs to users with
    # ON DELETE SET NULL, but the override row is org-scoped — wipe
    # before users so the count reflects every override the org owned
    # (rather than depending on FK semantics to nulls vs. cascades).
    counts["org_feature_overrides"] = (
        await db.execute(
            delete(OrgFeatureOverride).where(OrgFeatureOverride.org_id == org_id)
        )
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
