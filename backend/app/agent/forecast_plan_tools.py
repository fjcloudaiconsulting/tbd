"""The forecast plan tools (TBD-668): read a period's plan; add, bulk-set,
edit and delete its items; populate and refresh it from sources; activate,
revert, discard and copy it.

Each change tool is route-bound (``app.agent.route_tool``): execute runs the
REST handler. Every one is ``sensitive`` (the person confirms it in the app):
an item's planned amount is what the forecast, the budgets seeded from it and
the dashboard count by, so adding, editing, deleting, populating, refreshing
or copying items changes the amounts the plan states; activate and revert
switch whether the plan may change at all, and discard deletes every item.

Populate, refresh and copy create the period's plan when it has none: the
preview names that row. Their item rows are the ones the services'
read-only halves (``plan_populate``, ``plan_copy``) decide. REST auto-creates
an open period for an org that has none; a preview refuses instead.
"""
from __future__ import annotations

import datetime

from sqlalchemy import select

from app.agent.budget_tools import _currency, _names, _open_period
from app.agent.registry import Change, Preview, ToolContext, ToolError, ToolSpec, register
from app.agent.route_tool import route_tool
from app.agent.sensitive_tools import CONFIRM
from app.agent.tools import PeriodArgs, _resolve_period_start

from app.models.billing import BillingPeriod
from app.models.forecast_plan import ForecastItemType, ForecastPlan, ForecastPlanItem, ItemSource, PlanStatus
from app.models.user import Role
from app.routers import forecast_plans as forecast_plans_router
from app.services import billing_service
from app.services import forecast_plan_service as svc
from app.services.exceptions import NotFoundError, ValidationError
from app.services.feature_gate import Feature
from app.services.settings_service import FORECAST_GRANULARITY_SUBCATEGORY, get_forecast_input_granularity

ROUTER = forecast_plans_router.router
FORECAST = Feature.FORECAST
ITEMS = "forecast_plan_items"
DRAFT_ONLY = "Only a draft plan changes."

# An item row as ``(category_id, type, planned_amount, source)``.
Row = tuple[int, str, object, str]


async def _get(ctx: ToolContext, args: PeriodArgs) -> dict | None:
    start = await _resolve_period_start(ctx, args.period_start)
    out = await svc.get_plan_for_period(ctx.db, ctx.org_id, period_start=start)
    return None if out is None else out.model_dump(mode="json")


register(ToolSpec(
    name="forecast_plan_get", risk="read", args=PeriodArgs, product_area=FORECAST,
    min_role=Role.MEMBER, mirrors_route=("GET", "/api/v1/forecast-plans/current"),
    description="A billing period's forecast plan and its items with ids, or null (default: the open period).",
    run=_get,
))


# ── shared preview pieces ─────────────────────────────────────────────────

async def _period(ctx: ToolContext, start: datetime.date | None) -> BillingPeriod:
    if start is None:
        return await _open_period(ctx)
    row = await billing_service._find_period_by_start(ctx.db, ctx.org_id, start)
    if row is None:
        raise ValidationError("Billing period not found")
    return row


def _locked(*where):
    return (
        select(ForecastPlan).where(ForecastPlan.org_id == where[0], *where[1:])
        .with_for_update().execution_options(populate_existing=True)
    )


async def _plan(ctx: ToolContext, plan_id: int) -> ForecastPlan:
    row = await ctx.db.scalar(_locked(ctx.org_id, ForecastPlan.id == plan_id))
    if row is None:
        raise NotFoundError("Forecast plan")
    return row


async def _plan_of(ctx: ToolContext, period: BillingPeriod) -> ForecastPlan | None:
    return await ctx.db.scalar(_locked(ctx.org_id, ForecastPlan.billing_period_id == period.id))


async def _items(ctx: ToolContext, plan: ForecastPlan | None) -> list[ForecastPlanItem]:
    if plan is None:
        return []
    return list((await ctx.db.scalars(
        select(ForecastPlanItem).where(ForecastPlanItem.plan_id == plan.id)
        .order_by(ForecastPlanItem.id).execution_options(populate_existing=True)
    )).all())


def _inserts(plan_ref, rows: list[Row], currency: str | None, first: int) -> list[Change]:
    out: list[Change] = []
    for k, (category_id, type_, amount, source) in enumerate(rows, first):
        gid = f"new:{k}"
        out += [
            Change(ITEMS, gid, "plan_id", None, plan_ref),
            Change(ITEMS, gid, "category_id", None, category_id),
            Change(ITEMS, gid, "type", None, type_),
            Change(ITEMS, gid, "planned_amount", None, f"{amount:.2f}", currency),
            Change(ITEMS, gid, "source", None, source),
        ]
    return out


def _deletes(items: list[ForecastPlanItem], currency: str | None) -> list[Change]:
    return [
        c for i in items for c in (
            Change(ITEMS, i.id, "category_id", i.category_id, None),
            Change(ITEMS, i.id, "type", i.type.value, None),
            Change(ITEMS, i.id, "planned_amount", f"{i.planned_amount:.2f}", None, currency),
            Change(ITEMS, i.id, "source", i.source.value, None),
        )
    ]


async def _context(ctx: ToolContext, plan_id, period_start, category_ids, gone=()) -> dict:
    out = {"plan_id": plan_id, "period_start": period_start.isoformat() if period_start else None,
           "categories": await _names(ctx, set(category_ids))}
    if gone:
        out["deleted"] = [{"entity": ITEMS, "id": i.id} for i in gone]
    return out


async def _filled(
    ctx: ToolContext, verb: str, period: BillingPeriod, plan: ForecastPlan | None,
    gone: list[ForecastPlanItem], rows: list[Row],
) -> Preview:
    """A populate, refresh or copy: the plan row it creates, if any, the items
    it deletes and the items it adds."""
    currency = await _currency(ctx)
    changes: list[Change] = []
    ref = plan.id if plan is not None else "new:1"
    if plan is None:
        changes += [Change("forecast_plans", ref, "billing_period_id", None, period.id),
                    Change("forecast_plans", ref, "status", None, PlanStatus.DRAFT.value)]
    changes += _deletes(gone, currency) + _inserts(ref, rows, currency, 2 if plan is None else 1)
    if not changes:
        raise ToolError("no_change", "the plan would not change")
    return Preview(
        summary=(f"{verb} the forecast plan for the period starting {period.start_date.isoformat()}: "
                 f"{len(rows)} item(s) added" + (f", {len(gone)} removed" if gone else "")),
        changes=changes,
        warnings=["Creates the period's plan as a draft."] if plan is None else [],
        context=await _context(ctx, plan and plan.id, period.start_date,
                               [r[0] for r in rows] + [i.category_id for i in gone], gone),
    )


# ── populate, refresh, copy ───────────────────────────────────────────────

async def _populate_preview(ctx: ToolContext, args) -> Preview:
    period = await _period(ctx, args.period_start)
    plan = await _plan_of(ctx, period)
    if plan is not None:
        svc._require_draft(plan)
    rows, _ = await svc.plan_populate(ctx.db, ctx.org_id, period, await _items(ctx, plan))
    return await _filled(ctx, "Populate", period, plan, [], [(c, t.value, a, s.value) for c, t, a, s in rows])


async def _refresh_preview(ctx: ToolContext, args) -> Preview:
    period = await _period(ctx, args.period_start)
    plan = await _plan_of(ctx, period)
    if plan is not None:
        svc._require_draft(plan)
    items = await _items(ctx, plan)
    pre = await svc.sub_pairs(ctx.db, ctx.org_id, items)
    kept = [i for i in items if i.source == ItemSource.MANUAL]
    rows, _ = await svc.plan_populate(ctx.db, ctx.org_id, period, kept, pre_existing_sub_pairs=pre)
    gone = [i for i in items if i.source != ItemSource.MANUAL]
    return await _filled(ctx, "Refresh", period, plan, gone, [(c, t.value, a, s.value) for c, t, a, s in rows])


async def _copy_preview(ctx: ToolContext, args) -> Preview:
    target_period = await _period(ctx, args.target_period_start)
    source_period = await _period(ctx, args.source_period_start)
    source = await ctx.db.scalar(select(ForecastPlan).where(
        ForecastPlan.org_id == ctx.org_id, ForecastPlan.billing_period_id == source_period.id))
    source_items = await _items(ctx, source)
    if not source_items:
        raise ValidationError("Source period has no plan to copy")
    target = await _plan_of(ctx, target_period)
    if target is not None:
        svc._require_draft(target)
    picked = await svc.plan_copy(ctx.db, ctx.org_id, source_items, await _items(ctx, target))
    rows = [(i.category_id, i.type.value, i.planned_amount, ItemSource.MANUAL.value) for i in picked]
    return await _filled(ctx, "Copy into", target_period, target, [], rows)


_SOURCES = "recurring items and the last 3 months of history"
register(route_tool(
    name="forecast_plan_populate", risk="sensitive",
    router=ROUTER, method="POST", path="/api/v1/forecast-plans/populate",
    fields=("period_start",), product_area=FORECAST, min_role=Role.MEMBER,
    description=(f"Add plan items from {_SOURCES} for categories the plan lacks "
                 f"(default: the open period; creates the plan if needed). {CONFIRM}"),
    preview=_populate_preview,
))
register(route_tool(
    name="forecast_plan_refresh", risk="sensitive",
    router=ROUTER, method="POST", path="/api/v1/forecast-plans/refresh-from-sources",
    fields=("period_start",), product_area=FORECAST, min_role=Role.MEMBER,
    description=(f"Replace the plan's generated items with fresh ones from {_SOURCES}; "
                 f"items the user set by hand stay. {CONFIRM}"),
    preview=_refresh_preview,
))
register(route_tool(
    name="forecast_plan_copy", risk="sensitive",
    router=ROUTER, method="POST", path="/api/v1/forecast-plans/copy",
    fields=("source_period_start", "target_period_start"), required=("source_period_start",),
    product_area=FORECAST, min_role=Role.MEMBER,
    description=("Copy a period's plan items into another period's plan (default: the open "
                 f"period), skipping categories it already plans. {CONFIRM}"),
    preview=_copy_preview,
))


# ── items ─────────────────────────────────────────────────────────────────

async def _upsert(ctx: ToolContext, plan_id: int, wanted: list[tuple[int, str, object]]) -> Preview:
    plan = await _plan(ctx, plan_id)
    svc._require_draft(plan)
    sub_mode = await get_forecast_input_granularity(ctx.db, ctx.org_id) == FORECAST_GRANULARITY_SUBCATEGORY
    for category_id, type_ in dict.fromkeys((c, t) for c, t, _ in wanted):
        await svc._validate_category_granularity(
            ctx.db, ctx.org_id, category_id, ForecastItemType(type_), subcategory_mode=sub_mode)
    last = {(c, t): a for c, t, a in wanted}  # a repeated key: the last amount wins, as in bulk_upsert
    have = {(i.category_id, i.type.value): i for i in await _items(ctx, plan)}
    currency = await _currency(ctx)
    changes: list[Change] = []
    new: list[Row] = []
    for (c, t), amount in last.items():
        item = have.get((c, t))
        if item is None:
            new.append((c, t, amount, ItemSource.MANUAL.value))
            continue
        if item.planned_amount != amount:
            changes.append(Change(ITEMS, item.id, "planned_amount", f"{item.planned_amount:.2f}",
                                  f"{amount:.2f}", currency))
        if item.source != ItemSource.MANUAL:
            changes.append(Change(ITEMS, item.id, "source", item.source.value, ItemSource.MANUAL.value))
    changes += _inserts(plan.id, new, currency, 1)
    if not changes:
        raise ToolError("no_change", "the plan already has these amounts")
    return Preview(
        summary=f"Set {len(last)} forecast plan item(s) on plan {plan.id}",
        changes=changes, context=await _context(ctx, plan.id, None, [c for c, _ in last]),
    )


async def _add_preview(ctx: ToolContext, args) -> Preview:
    return await _upsert(ctx, args.plan_id, [(args.category_id, args.type, args.planned_amount)])


async def _bulk_preview(ctx: ToolContext, args) -> Preview:
    return await _upsert(ctx, args.plan_id, [(i.category_id, i.type, i.planned_amount) for i in args.items])


async def _item(ctx: ToolContext, args) -> tuple[ForecastPlan, ForecastPlanItem]:
    plan = await _plan(ctx, args.plan_id)
    svc._require_draft(plan)
    item = next((i for i in await _items(ctx, plan) if i.id == args.item_id), None)
    if item is None:
        raise NotFoundError("Forecast plan item")
    return plan, item


async def _update_preview(ctx: ToolContext, args) -> Preview:
    plan, item = await _item(ctx, args)
    currency = await _currency(ctx)
    changes = []
    if item.planned_amount != args.planned_amount:
        changes.append(Change(ITEMS, item.id, "planned_amount", f"{item.planned_amount:.2f}",
                              f"{args.planned_amount:.2f}", currency))
    if item.source != ItemSource.MANUAL:
        changes.append(Change(ITEMS, item.id, "source", item.source.value, ItemSource.MANUAL.value))
    if not changes:
        raise ToolError("no_change", "the item already has this amount")
    return Preview(summary=f"Change forecast plan item {item.id}", changes=changes,
                   context=await _context(ctx, plan.id, None, [item.category_id]))


async def _delete_preview(ctx: ToolContext, args) -> Preview:
    plan, item = await _item(ctx, args)
    return Preview(
        summary=f"Delete forecast plan item {item.id}",
        changes=_deletes([item], await _currency(ctx)),
        context=await _context(ctx, plan.id, None, [item.category_id], [item]),
    )


_ADD = "Manual items (added or set by the user) survive a refresh."
register(route_tool(
    name="forecast_plan_item_add", risk="sensitive",
    router=ROUTER, method="POST", path="/api/v1/forecast-plans/{plan_id}/items",
    fields=("category_id", "type", "planned_amount"), required=("category_id", "type", "planned_amount"),
    product_area=FORECAST, min_role=Role.MEMBER,
    description=f"Set a draft plan's planned amount for a category and type (adds or replaces). {_ADD} {CONFIRM}",
    preview=_add_preview,
))
register(route_tool(
    name="forecast_plan_items_bulk", risk="sensitive",
    router=ROUTER, method="POST", path="/api/v1/forecast-plans/{plan_id}/items/bulk",
    fields=("items",), required=("items",), product_area=FORECAST, min_role=Role.MEMBER,
    description=f"forecast_plan_item_add for several items at once. {CONFIRM}",
    preview=_bulk_preview,
))
register(route_tool(
    name="forecast_plan_item_update", risk="sensitive",
    router=ROUTER, method="PUT", path="/api/v1/forecast-plans/{plan_id}/items/{item_id}",
    fields=("planned_amount",), required=("planned_amount",), product_area=FORECAST, min_role=Role.MEMBER,
    description=f"Change one draft plan item's planned amount. {_ADD} {CONFIRM}",
    preview=_update_preview,
))
register(route_tool(
    name="forecast_plan_item_delete", risk="sensitive",
    router=ROUTER, method="DELETE", path="/api/v1/forecast-plans/{plan_id}/items/{item_id}",
    fields=(), product_area=FORECAST, min_role=Role.MEMBER,
    description=f"Delete one item of a draft plan. {CONFIRM}",
    preview=_delete_preview,
))


# ── status ────────────────────────────────────────────────────────────────

async def _status(ctx: ToolContext, plan: ForecastPlan, to: PlanStatus) -> Preview:
    return Preview(
        summary=f"Mark forecast plan {plan.id} {to.value}",
        changes=[Change("forecast_plans", plan.id, "status", plan.status.value, to.value)],
        context={"plan_id": plan.id},
    )


async def _activate_preview(ctx: ToolContext, args) -> Preview:
    plan = await _plan(ctx, args.plan_id)
    svc._require_draft(plan)
    if not await _items(ctx, plan):
        raise ValidationError("Cannot activate an empty plan")
    return await _status(ctx, plan, PlanStatus.ACTIVE)


async def _revert_preview(ctx: ToolContext, args) -> Preview:
    plan = await _plan(ctx, args.plan_id)
    if plan.status != PlanStatus.ACTIVE:
        raise ValidationError("Plan is already a draft")
    return await _status(ctx, plan, PlanStatus.DRAFT)


async def _discard_preview(ctx: ToolContext, args) -> Preview:
    plan = await _plan(ctx, args.plan_id)
    svc._require_draft(plan)
    items = await _items(ctx, plan)
    if not items:
        raise ToolError("no_change", "the plan has no items")
    return Preview(
        summary=f"Delete all {len(items)} item(s) of forecast plan {plan.id}",
        changes=_deletes(items, await _currency(ctx)),
        warnings=["A delete cannot be undone."],
        context=await _context(ctx, plan.id, None, [i.category_id for i in items], items),
    )


for _name, _path, _text, _preview in (
    ("forecast_plan_activate", "activate", f"Finalise a draft plan; an active plan is read-only. {CONFIRM}",
     _activate_preview),
    ("forecast_plan_revert", "revert", f"Return an active plan to draft so it can change. {CONFIRM}",
     _revert_preview),
    ("forecast_plan_discard", "discard", f"Delete every item of a draft plan. {CONFIRM}", _discard_preview),
):
    register(route_tool(
        name=_name, risk="sensitive",
        router=ROUTER, method="POST", path=f"/api/v1/forecast-plans/{{plan_id}}/{_path}",
        fields=(), product_area=FORECAST, min_role=Role.MEMBER, description=_text, preview=_preview,
    ))
