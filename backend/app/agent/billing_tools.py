"""The billing-period tools (TBD-667): two reads and the four writes on
``/api/v1/settings`` that shape the org's periods.

All four writes are admin-only (the routes call ``_require_admin``) and
``sensitive``: a period boundary decides which spend every budget, report and
forecast counts, and none of them has an inverse a revert could stage. Each
is route-bound; each preview names every row it writes (fenced by
``tests/agent/test_agent_honesty.py``). A route that would auto-create the
open period (``get_current_period``) is refused instead when the org has
none, so no preview has to promise a row the route invents.
"""
from __future__ import annotations

import datetime

from sqlalchemy import or_, select

from app.agent.registry import Change, Preview, ToolContext, ToolError, register
from app.agent.route_tool import route_tool
from app.agent.sensitive_tools import CONFIRM, _iso
from app.models import Budget, Organization
from app.models.billing import BillingPeriod
from app.models.user import Role
from app.routers import settings as settings_router
from app.services import billing_service
from app.services.exceptions import ValidationError

ROUTER = settings_router.router
PERIODS = "billing_periods"


async def _open_period(ctx: ToolContext) -> BillingPeriod:
    period = await billing_service.find_open_period(ctx.db, ctx.org_id)
    if period is None:
        raise ToolError("no_open_period", "the organization has no open billing period")
    return period


register(route_tool(
    name="billing_cycle_get", risk="read",
    router=ROUTER, method="GET", path="/api/v1/settings/billing-cycle",
    fields=(), product_area=None, min_role=Role.MEMBER,
    description="Read the organization's billing cycle day (the day each billing period starts).",
))
register(route_tool(
    name="billing_periods_list", risk="read",
    router=ROUTER, method="GET", path="/api/v1/settings/billing-periods",
    fields=(), product_area=None, min_role=Role.MEMBER,
    description="List the organization's 24 most recent billing periods (an open one has no end_date).",
))


# ── billing_cycle_update ──────────────────────────────────────────────────

async def _cycle_preview(ctx: ToolContext, args) -> Preview:
    org = await ctx.db.scalar(
        select(Organization).where(Organization.id == ctx.org_id).execution_options(populate_existing=True)
    )
    if org.billing_cycle_day == args.billing_cycle_day:
        raise ToolError("no_change", "the billing cycle day already has this value")
    period = await _open_period(ctx)
    return Preview(
        summary=f"Change the billing cycle day from {org.billing_cycle_day} to {args.billing_cycle_day}",
        changes=[Change("organizations", org.id, "billing_cycle_day", org.billing_cycle_day,
                        args.billing_cycle_day)],
        warnings=["Applies from the next billing period: the open period keeps its dates."],
        context={"period_start": period.start_date.isoformat()},
    )


register(route_tool(
    name="billing_cycle_update", risk="sensitive",
    router=ROUTER, method="PUT", path="/api/v1/settings/billing-cycle",
    fields=("billing_cycle_day",), required=("billing_cycle_day",),
    product_area=None, min_role=Role.ADMIN,
    description=(
        "Change the day of the month billing periods start on (1-28). It applies from the next "
        f"period; the open period is not changed. Admins only. {CONFIRM}"
    ),
    preview=_cycle_preview,
))


# ── billing_period_create ─────────────────────────────────────────────────

async def _create_present(ctx: ToolContext, _row, args, changes: list[Change]) -> Preview:
    return Preview(summary="Create a billing period", changes=changes)


register(route_tool(
    name="billing_period_create", risk="sensitive",
    router=ROUTER, method="POST", path="/api/v1/settings/billing-period",
    fields=("start_date", "end_date"), required=("start_date",),
    product_area=None, min_role=Role.ADMIN,
    description=(
        "Create a billing period with explicit dates (no end_date: an open period). Refused when "
        f"it overlaps an existing period. Admins only. {CONFIRM}"
    ),
    entity=BillingPeriod, present=_create_present,
))


# ── billing_periods_ensure_future ─────────────────────────────────────────

async def _ensure_preview(ctx: ToolContext, args) -> Preview:
    sent = "count" in args.model_fields_set
    count = billing_service.clamp_future_count(args.count if sent else billing_service.FUTURE_PERIODS_DEFAULT)
    current = await _open_period(ctx)
    planned = await billing_service.plan_future_periods(ctx.db, ctx.org_id, current.start_date, count)
    if not planned:
        raise ToolError("no_change", "the upcoming billing periods already exist")
    changes = []
    for k, (start, end) in enumerate(planned, 1):
        changes += [Change(PERIODS, f"new:{k}", "start_date", None, _iso(start)),
                    Change(PERIODS, f"new:{k}", "end_date", None, _iso(end))]
    return Preview(summary=f"Create {len(planned)} upcoming billing period(s)", changes=changes)


register(route_tool(
    name="billing_periods_ensure_future", risk="sensitive",
    router=ROUTER, method="POST", path="/api/v1/settings/billing-periods/ensure-future",
    fields=("count",), product_area=None, min_role=Role.ADMIN,
    description=(
        "Create planning stubs for the next count billing periods after the open one (1-6, "
        f"default 3); periods that already exist are skipped. Admins only. {CONFIRM}"
    ),
    preview=_ensure_preview,
))


# ── billing_period_close ──────────────────────────────────────────────────
#
# ``close_period``, read-only: the close date (yesterday by default) is
# clamped to the day before the next existing period; the open period and its
# budgets end there; the period starting the next day is revived (it and its
# budgets become open) or inserted.

async def _close_preview(ctx: ToolContext, args) -> Preview:
    db, org_id, today = ctx.db, ctx.org_id, datetime.date.today()
    current = await _open_period(ctx)
    requested = args.close_date if args.close_date is not None else today - datetime.timedelta(days=1)
    if requested > today:
        raise ValidationError("Close date cannot be in the future")
    if requested < current.start_date:
        raise ValidationError("Close date cannot be before the period start date")
    s0 = await billing_service._next_period_start(db, org_id, after=current.start_date)
    resolved = s0 - datetime.timedelta(days=1) if s0 is not None and s0 <= requested else requested
    new_start = resolved + datetime.timedelta(days=1)

    async def budgets(start: datetime.date, differs) -> list[Budget]:
        return list((await db.scalars(
            select(Budget).where(Budget.org_id == org_id, Budget.period_start == start, differs)
            .order_by(Budget.id).execution_options(populate_existing=True)
        )).all())

    changes = [Change(PERIODS, current.id, "end_date", None, _iso(resolved))]
    changes += [
        Change("budgets", b.id, "period_end", _iso(b.period_end), _iso(resolved))
        for b in await budgets(current.start_date, or_(Budget.period_end.is_(None), Budget.period_end != resolved))
    ]
    revived = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.org_id == org_id, BillingPeriod.start_date == new_start)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if revived is None:
        changes.append(Change(PERIODS, "new:1", "start_date", None, _iso(new_start)))
    else:
        if revived.end_date is not None:
            changes.append(Change(PERIODS, revived.id, "end_date", _iso(revived.end_date), None))
        changes += [
            Change("budgets", b.id, "period_end", _iso(b.period_end), None)
            for b in await budgets(new_start, Budget.period_end.is_not(None))
        ]
    warnings = ["Closing a period cannot be undone here."]
    if resolved != requested:
        warnings.append(f"The close date is moved to {resolved.isoformat()}, the day before the next period.")
    return Preview(
        summary=f"Close the billing period starting {current.start_date.isoformat()} on {resolved.isoformat()}",
        changes=changes, warnings=warnings,
        context={"period_start": current.start_date.isoformat(), "new_period_start": new_start.isoformat()},
    )


register(route_tool(
    name="billing_period_close", risk="sensitive",
    router=ROUTER, method="POST", path="/api/v1/settings/billing-period/close",
    fields=("close_date",), product_area=None, min_role=Role.ADMIN,
    description=(
        "Close the open billing period on close_date (default yesterday, never in the future) "
        "and open the next one the day after. A close past the next existing period is moved "
        f"to the day before it. Admins only. {CONFIRM}"
    ),
    preview=_close_preview,
))
