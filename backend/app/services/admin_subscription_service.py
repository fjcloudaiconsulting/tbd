"""Cross-org subscription read API (L4.5).

Three concerns, kept out of the router for testability:

- ``list_subscriptions`` — paginated table feed for ``/admin/subscriptions``.
- ``get_subscription_detail`` — drill-down payload including org snapshot,
  plan info and active feature overrides (from L4.11).
- ``aggregate_revenue_kpis`` — pulse strip totals for the list header.

This surface is **platform-scoped**, not org-scoped. The route gate is
``subscriptions.view`` (superadmin short-circuits today; future
non-superadmin roles via L4.8 can be assigned the permission without
touching this file).

Real payments aren't integrated yet (L2.2 is parked). Every dollar
figure in the KPI envelope is mocked at ``$0`` and clearly flagged via
the ``mock_revenue`` boolean so the FE can render the right
disclosure. When L2 lands, the aggregator switches from mock-zero to
real ``Stripe.Subscription``/``Paddle.Subscription`` totals without a
schema change.
"""
from __future__ import annotations

import datetime
from typing import Any, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app._time import utcnow_naive
from app.models.feature_override import OrgFeatureOverride
from app.models.subscription import (
    BillingInterval,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.models.user import Organization, User
from app.services.admin_users_search_service import _normalize_like
from app.services.exceptions import NotFoundError


# How far ahead "trial expiring soon" looks. Locked at 7 days to match
# the L4.5 spec; surface in the response so the FE can label the tile
# without having to know the constant.
TRIAL_EXPIRING_WINDOW_DAYS = 7
# Lookback window for "new signups". Same as the L4.2 dashboard so the
# tiles stay consistent across admin pages.
SIGNUPS_LOOKBACK_DAYS = 7


def _row_to_list_item(row: Any) -> dict[str, Any]:
    """Map a flat result row from the list SELECT to the JSON shape.

    The router calls ``SubscriptionListItem.model_validate`` on each
    dict so any missing field surfaces as a 500 with a Pydantic
    error rather than a silent shape drift.
    """
    return {
        "subscription_id": row.subscription_id,
        "org_id": row.org_id,
        "org_name": row.org_name,
        "plan_id": row.plan_id,
        "plan_slug": row.plan_slug,
        "plan_name": row.plan_name,
        "status": row.status.value if hasattr(row.status, "value") else row.status,
        "billing_interval": (
            row.billing_interval.value
            if hasattr(row.billing_interval, "value")
            else row.billing_interval
        ),
        "trial_start": row.trial_start,
        "trial_end": row.trial_end,
        "current_period_start": row.current_period_start,
        "current_period_end": row.current_period_end,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


async def list_subscriptions(
    db: AsyncSession,
    *,
    status_filter: Optional[str] = None,
    plan_filter: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Paginated, filterable list of every subscription on the platform.

    Single SELECT with INNER JOINs to ``organizations`` and ``plans`` —
    each subscription has a NOT NULL ``org_id`` and ``plan_id`` FK so
    INNER JOIN is correct and lets the DB plan the filter pushdown.

    Filters:

    - ``status_filter``: one of the ``SubscriptionStatus`` values.
      Unknown values are rejected at the route boundary (Literal) and
      raise ``ValueError`` here defensively for direct service callers.
    - ``plan_filter``: plan ``slug`` (string, exact match). Frontend
      sources the slug from ``/api/v1/plans`` so typos are impossible.
    - ``q``: ILIKE-style substring on ``Organization.name`` OR
      ``Plan.slug``. Keeps a single search box useful regardless of
      whether ops types an org name or a plan slug.

    Pagination is offset-based to match ``/admin/orgs``; cursor-based
    pagination is out of scope here and would require an index on
    ``(created_at, id)`` to avoid the same drift the L4.3 review
    closed.
    """
    where_clauses = []
    if status_filter is not None:
        # The Literal on the router rejects garbage; this is the
        # defensive call-direct path.
        try:
            status_enum = SubscriptionStatus(status_filter)
        except ValueError as exc:
            raise ValueError(
                f"Unknown subscription status: {status_filter!r}"
            ) from exc
        where_clauses.append(Subscription.status == status_enum)
    if plan_filter:
        where_clauses.append(Plan.slug == plan_filter)
    if q:
        # Escape LIKE metacharacters so a raw query like ``%`` or ``_``
        # can't widen the match. Reuses the helper from the cross-org
        # user search (admin_users_search_service) to keep one source
        # of truth for the escape rules.
        pattern = f"%{_normalize_like(q.strip())}%"
        where_clauses.append(
            or_(
                Organization.name.ilike(pattern, escape="\\"),
                Plan.slug.ilike(pattern, escape="\\"),
                Plan.name.ilike(pattern, escape="\\"),
            )
        )

    base = (
        select(
            Subscription.id.label("subscription_id"),
            Subscription.org_id.label("org_id"),
            Organization.name.label("org_name"),
            Subscription.plan_id.label("plan_id"),
            Plan.slug.label("plan_slug"),
            Plan.name.label("plan_name"),
            Subscription.status,
            Subscription.billing_interval,
            Subscription.trial_start,
            Subscription.trial_end,
            Subscription.current_period_start,
            Subscription.current_period_end,
            Subscription.created_at,
            Subscription.updated_at,
        )
        .select_from(Subscription)
        .join(Organization, Organization.id == Subscription.org_id)
        .join(Plan, Plan.id == Subscription.plan_id)
    )
    count_q = (
        select(func.count())
        .select_from(Subscription)
        .join(Organization, Organization.id == Subscription.org_id)
        .join(Plan, Plan.id == Subscription.plan_id)
    )
    for clause in where_clauses:
        base = base.where(clause)
        count_q = count_q.where(clause)

    total = (await db.execute(count_q)).scalar_one()
    rows = (
        await db.execute(
            base.order_by(Subscription.created_at.desc(), Subscription.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    return {
        "items": [_row_to_list_item(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _plan_to_dict(plan: Plan) -> dict[str, Any]:
    return {
        "id": plan.id,
        "slug": plan.slug,
        "name": plan.name,
        "description": plan.description or "",
        # Decimal → str — pydantic v2 doesn't auto-stringify Decimal and
        # the FE convention is "money as string" (see
        # project_decimal_typing_debt.md).
        "price_monthly": str(plan.price_monthly),
        "price_yearly": str(plan.price_yearly),
        "max_users": plan.max_users,
        "retention_days": plan.retention_days,
        "features": plan.features or {},
        "is_custom": plan.is_custom,
        "is_active": plan.is_active,
    }


async def get_subscription_detail(
    db: AsyncSession, *, subscription_id: int
) -> dict[str, Any]:
    """Drill-down payload for ``/admin/subscriptions/[id]``.

    Returns the subscription row, the linked org (with a member count),
    the plan, and the org's active feature overrides (from L4.11) so
    the admin can see at a glance whether the org has any extra
    capabilities beyond the plan.

    The ``feature_overrides`` list is read-only here. Grant / revoke
    flow still lives on ``/admin/orgs/[id]`` (PR #109) — L4.5 does not
    duplicate that mutation surface.
    """
    sub = (
        await db.execute(
            select(Subscription).where(Subscription.id == subscription_id)
        )
    ).scalar_one_or_none()
    if sub is None:
        raise NotFoundError("Subscription")

    org = (
        await db.execute(
            select(Organization).where(Organization.id == sub.org_id)
        )
    ).scalar_one_or_none()
    if org is None:
        # Should be impossible (FK NOT NULL), but guard so the
        # response shape stays sane for direct DB tampering scenarios.
        raise NotFoundError("Organization for subscription")

    plan = (
        await db.execute(select(Plan).where(Plan.id == sub.plan_id))
    ).scalar_one_or_none()

    member_count = await db.scalar(
        select(func.count()).select_from(User).where(User.org_id == org.id)
    ) or 0

    override_rows = (
        await db.execute(
            select(OrgFeatureOverride)
            .where(OrgFeatureOverride.org_id == org.id)
            .order_by(OrgFeatureOverride.feature_key.asc())
        )
    ).scalars().all()

    now = utcnow_naive()
    overrides: list[dict[str, Any]] = []
    for row in override_rows:
        is_expired = row.expires_at is not None and row.expires_at <= now
        overrides.append(
            {
                "feature_key": row.feature_key,
                "value": bool(row.value),
                "set_at": row.set_at,
                "expires_at": row.expires_at,
                "is_expired": is_expired,
                "note": row.note,
            }
        )

    return {
        "subscription_id": sub.id,
        "org": {
            "id": org.id,
            "name": org.name,
            "billing_cycle_day": org.billing_cycle_day,
            "created_at": org.created_at,
            "member_count": int(member_count),
        },
        "plan": _plan_to_dict(plan) if plan is not None else None,
        "status": sub.status.value,
        "billing_interval": sub.billing_interval.value,
        "trial_start": sub.trial_start,
        "trial_end": sub.trial_end,
        "current_period_start": sub.current_period_start,
        "current_period_end": sub.current_period_end,
        "created_at": sub.created_at,
        "updated_at": sub.updated_at,
        "feature_overrides": overrides,
        "mock_revenue_amount": "0.00",
        "mock_revenue": True,
    }


async def aggregate_revenue_kpis(
    db: AsyncSession,
    *,
    now: Optional[datetime.datetime] = None,
) -> dict[str, Any]:
    """Pulse-strip totals for the ``/admin/subscriptions`` header.

    Six counts plus a per-plan distribution. Computed sequentially on
    the request session — SQLAlchemy's ``AsyncSession`` is not safe for
    concurrent use across tasks (same constraint that drives
    ``admin_dashboard_service`` to serialise its KPI reads).

    ``mock_mrr`` and ``mock_arr`` are hard-pinned to ``"0.00"`` until
    L2.2 wires real billing. ``mock_revenue`` stays ``True`` so the FE
    keeps surfacing the disclosure even after a typo or test-fixture
    that accidentally returns a non-zero string here.
    """
    base_now = now or datetime.datetime.now(datetime.timezone.utc)
    # Strip tzinfo to align with ``DateTime`` columns in the schema,
    # which are stored TZ-naive (UTC by convention — see ``app/_time.py``).
    naive_now = base_now.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    today = naive_now.date()
    signups_cutoff = naive_now - datetime.timedelta(days=SIGNUPS_LOOKBACK_DAYS)
    trial_window_end = today + datetime.timedelta(days=TRIAL_EXPIRING_WINDOW_DAYS)

    total_subs = await db.scalar(
        select(func.count()).select_from(Subscription)
    ) or 0
    active = await db.scalar(
        select(func.count())
        .select_from(Subscription)
        .where(Subscription.status == SubscriptionStatus.ACTIVE)
    ) or 0
    trial = await db.scalar(
        select(func.count())
        .select_from(Subscription)
        .where(Subscription.status == SubscriptionStatus.TRIALING)
    ) or 0
    past_due = await db.scalar(
        select(func.count())
        .select_from(Subscription)
        .where(Subscription.status == SubscriptionStatus.PAST_DUE)
    ) or 0
    cancelled = await db.scalar(
        select(func.count())
        .select_from(Subscription)
        .where(Subscription.status == SubscriptionStatus.CANCELED)
    ) or 0
    signups_7d = await db.scalar(
        select(func.count())
        .select_from(Subscription)
        .where(Subscription.created_at >= signups_cutoff)
    ) or 0
    # Trials expiring in the next 7 days: trial_end in [today, today+7]
    # AND status is still TRIALING (cancelled trials don't count).
    trial_expiring = await db.scalar(
        select(func.count())
        .select_from(Subscription)
        .where(Subscription.status == SubscriptionStatus.TRIALING)
        .where(Subscription.trial_end.is_not(None))
        .where(Subscription.trial_end >= today)
        .where(Subscription.trial_end <= trial_window_end)
    ) or 0

    # Plan distribution — LEFT JOIN so every (currently active) plan
    # appears even with zero subscriptions. Tie-break by slug so the
    # order is deterministic in tests.
    dist_rows = (
        await db.execute(
            select(
                Plan.id,
                Plan.slug,
                Plan.name,
                func.count(Subscription.id).label("subs"),
            )
            .select_from(Plan)
            .outerjoin(Subscription, Subscription.plan_id == Plan.id)
            .group_by(Plan.id, Plan.slug, Plan.name)
            .order_by(func.count(Subscription.id).desc(), Plan.slug.asc())
        )
    ).all()
    plan_distribution = [
        {
            "plan_id": r.id,
            "plan_slug": r.slug,
            "plan_name": r.name,
            "subscription_count": int(r.subs or 0),
        }
        for r in dist_rows
    ]

    return {
        "total_subscriptions": int(total_subs),
        "active": int(active),
        "trial": int(trial),
        "past_due": int(past_due),
        "cancelled": int(cancelled),
        "signups_last_7d": int(signups_7d),
        "trial_expiring_next_7d": int(trial_expiring),
        "plan_distribution": plan_distribution,
        "mock_mrr": "0.00",
        "mock_arr": "0.00",
        "mock_revenue": True,
        "generated_at": naive_now,
    }
