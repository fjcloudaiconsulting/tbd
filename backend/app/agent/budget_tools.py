"""The budget tools (TBD-666): create, seed from a forecast, copy from a
period, draft the next period, rebalance and delete.

Each is route-bound (``app.agent.route_tool``): execute runs the REST handler.
A create, a seed, a copy, the draft and a delete are ``sensitive``: they insert
or delete rows a revert cannot put back, so the person confirms them in the
app. A rebalance moves allocation between existing budgets and is a ``write``
with its own inverse (the same items, swapped back).

The seed, the copy and the draft first add the next billing periods
(``ensure_future_periods``, which every Budgets page visit also runs): the
preview names those stubs, planned by the same read-only
``billing_service.plan_future_periods``. REST auto-creates an open period for an
org that has none; a preview refuses instead, as the read tools do.
"""
from __future__ import annotations

import datetime

from sqlalchemy import select

from app.agent.registry import Change, Preview, ToolContext, ToolError, register
from app.agent.route_tool import CascadeDisclosure, route_tool
from app.agent.sensitive_tools import CONFIRM
from app.models import Budget, Category, Organization
from app.models.billing import BillingPeriod
from app.models.user import Role
from app.routers import budgets as budgets_router
from app.services import billing_service, budget_service, currency_service
from app.services.exceptions import NotFoundError, ValidationError
from app.services.feature_gate import Feature

ROUTER = budgets_router.router
BUDGETS = Feature.BUDGETS
WRITE_CONFIRM = "Returns a preview to confirm; nothing changes until it is confirmed."
STUBS = "Also adds the next billing periods (a Budgets page visit adds them too)."

# A period as ``(start_date, end_date, id)``; the id is None for a stub this call adds.
Period = tuple[datetime.date, datetime.date | None, int | None]


async def _currency(ctx: ToolContext) -> str | None:
    org = await ctx.db.get(Organization, ctx.org_id)
    return org.primary_currency or (
        await currency_service.resolve_currency_scope(ctx.db, org_id=ctx.org_id)
    ).get("currency")


async def _open_period(ctx: ToolContext) -> BillingPeriod:
    period = await billing_service.find_open_period(ctx.db, ctx.org_id)
    if period is None:
        # REST would create one: a preview names what it writes, so refuse.
        raise ToolError("no_open_period", "the organization has no open billing period")
    return period


async def _period(ctx: ToolContext, start: datetime.date, stubs) -> Period:
    """The period at exactly ``start``: an existing row, or a stub this call adds."""
    row = await billing_service._find_period_by_start(ctx.db, ctx.org_id, start)
    if row is not None:
        return row.start_date, row.end_date, row.id
    for s, e in stubs:
        if s == start:
            return s, e, None
    raise ValidationError("Billing period not found")


def _stub_changes(stubs) -> list[Change]:
    return [
        Change("billing_periods", f"new:{k}", f, None, v.isoformat())
        for k, (s, e) in enumerate(stubs, 1) for f, v in (("start_date", s), ("end_date", e))
    ]


def _budget_inserts(rows, period: Period, currency: str | None, first: int) -> list[Change]:
    """One inserted budget per ``(category_id, amount)``, as ``new:<first>``..."""
    start, end, _ = period
    out: list[Change] = []
    for k, (category_id, amount) in enumerate(rows, first):
        gid = f"new:{k}"
        out += [
            Change("budgets", gid, "category_id", None, category_id),
            Change("budgets", gid, "amount", None, f"{amount:.2f}", currency),
            Change("budgets", gid, "period_start", None, start.isoformat()),
        ]
        if end is not None:
            out.append(Change("budgets", gid, "period_end", None, end.isoformat()))
    return out


async def _names(ctx: ToolContext, ids) -> list[dict]:
    rows = await ctx.db.execute(
        select(Category.id, Category.name)
        .where(Category.id.in_(list(ids)), Category.org_id == ctx.org_id).order_by(Category.id)
    )
    return [{"category_id": i, "category_name": n} for i, n in rows.all()]


async def _seeded(ctx: ToolContext, verb: str, rows, period: Period, stubs) -> Preview:
    if not rows and not stubs:
        raise ToolError("no_change", "every category here already has a budget in that period")
    currency = await _currency(ctx)
    return Preview(
        summary=f"{verb} {len(rows)} budget(s) for the period starting {period[0].isoformat()}",
        changes=_stub_changes(stubs) + _budget_inserts(rows, period, currency, len(stubs) + 1),
        warnings=[STUBS] if stubs else [],
        context={"period_start": period[0].isoformat(),
                 "categories": await _names(ctx, [c for c, _ in rows])},
    )


# ── budgets_create ────────────────────────────────────────────────────────

async def _create_preview(ctx: ToolContext, args) -> Preview:
    if args.period_start is not None:
        period = await _period(ctx, args.period_start, ())
    else:
        row = await _open_period(ctx)
        period = (row.start_date, row.end_date, row.id)
    cat = await ctx.db.scalar(
        select(Category).where(Category.id == args.category_id, Category.org_id == ctx.org_id)
    )
    if cat is None:
        raise ValidationError("Invalid category")
    if cat.parent_id is not None:
        raise ValidationError("Budgets can only be set for master categories, not subcategories")
    taken = await ctx.db.scalar(select(Budget.id).where(
        Budget.org_id == ctx.org_id, Budget.category_id == cat.id, Budget.period_start == period[0],
    ))
    if taken:
        raise ToolError("conflict", "this category already has a budget in that period")
    currency = await _currency(ctx)
    return Preview(
        summary=f"Create a budget of {args.amount:.2f}" + (f" {currency}" if currency else "")
        + f" for the period starting {period[0].isoformat()}",
        changes=_budget_inserts([(cat.id, args.amount)], period, currency, 1),
        context={"category_name": cat.name, "period_start": period[0].isoformat()},
    )


register(route_tool(
    name="budgets_create", risk="sensitive",
    router=ROUTER, method="POST", path="/api/v1/budgets",
    fields=("category_id", "amount", "period_start"), required=("category_id", "amount"),
    product_area=BUDGETS, min_role=Role.MEMBER,
    description=(
        "Create a budget for one master category in a billing period (default: the open "
        f"period). Subcategories cannot be budgeted. {CONFIRM}"
    ),
    preview=_create_preview,
))


# ── budgets_from_forecast ─────────────────────────────────────────────────

async def _from_forecast_preview(ctx: ToolContext, args) -> Preview:
    stubs: list = []
    if args.period_start is not None:
        stubs = await billing_service.plan_future_periods(ctx.db, ctx.org_id, (await _open_period(ctx)).start_date)
        row = await billing_service._find_period_by_start(ctx.db, ctx.org_id, args.period_start)
        if row is None:
            await _period(ctx, args.period_start, stubs)  # not a period at all: refused there
            raise ValidationError("No forecast plan exists for this period. Create one on the Forecasts page first.")
    else:
        row = await _open_period(ctx)
    amounts = await budget_service.forecast_budget_amounts(ctx.db, ctx.org_id, row)
    return await _seeded(ctx, "Create", list(amounts.items()), (row.start_date, row.end_date, row.id), stubs)


register(route_tool(
    name="budgets_from_forecast", risk="sensitive",
    router=ROUTER, method="POST", path="/api/v1/budgets/from-forecast",
    fields=("period_start",), product_area=BUDGETS, extra_areas=(Feature.FORECAST,),
    min_role=Role.MEMBER,
    description=(
        "Create budgets for a billing period (default: the open period) from its forecast "
        "plan: one per master category, summing its subcategories' planned expenses. "
        f"Categories that already have a budget there are skipped. {CONFIRM}"
    ),
    preview=_from_forecast_preview,
))


# ── budgets_copy_from_period ──────────────────────────────────────────────

async def _copy_preview(ctx: ToolContext, args) -> Preview:
    current = await _open_period(ctx)
    stubs = await billing_service.plan_future_periods(ctx.db, ctx.org_id, current.start_date)
    target = (
        await _period(ctx, args.target_period_start, stubs) if args.target_period_start is not None
        else (current.start_date, current.end_date, current.id)
    )
    source = await _period(ctx, args.source_period_start, stubs)
    rows = (await ctx.db.execute(
        select(Budget.category_id, Budget.amount)
        .where(Budget.org_id == ctx.org_id, Budget.period_start == source[0]).order_by(Budget.id)
    )).all()
    if not rows:
        raise ValidationError("Source period has no budgets to copy")
    taken = await budget_service._get_existing_budget_cat_ids(ctx.db, ctx.org_id, target[0])
    return await _seeded(ctx, "Copy", [(c, a) for c, a in rows if c not in taken], target, stubs)


register(route_tool(
    name="budgets_copy_from_period", risk="sensitive",
    router=ROUTER, method="POST", path="/api/v1/budgets/copy-from-period",
    fields=("source_period_start", "target_period_start"), required=("source_period_start",),
    product_area=BUDGETS, min_role=Role.MEMBER,
    description=(
        "Copy the budget amounts of one billing period into another (default target: the "
        f"open period). Categories already budgeted in the target are skipped. {CONFIRM}"
    ),
    preview=_copy_preview,
))


# ── budgets_draft_next ────────────────────────────────────────────────────

async def _draft_preview(ctx: ToolContext, args) -> Preview:
    stubs = await billing_service.plan_future_periods(ctx.db, ctx.org_id, (await _open_period(ctx)).start_date)
    start, _, _ = await _period(ctx, args.period_start, stubs)
    return Preview(
        summary=(
            f"Draft budgets for the period starting {start.isoformat()} from the last 3 months "
            "of spending. The draft creates no budget."
        ),
        changes=_stub_changes(stubs), warnings=[STUBS] if stubs else [],
        context={"period_start": start.isoformat()},
    )


register(route_tool(
    name="budgets_draft_next", risk="sensitive",
    router=ROUTER, method="POST", path="/api/v1/budgets/draft-next",
    fields=("period_start",), required=("period_start",),
    product_area=BUDGETS, min_role=Role.MEMBER,
    description=(
        "Suggest a budget amount per expense category for a billing period from its last 3 "
        "months of spending, skipping categories already budgeted there. It creates no "
        f"budget: create the ones the user accepts with budgets_create. {CONFIRM}"
    ),
    preview=_draft_preview,
))


# ── budgets_rebalance ─────────────────────────────────────────────────────
#
# The refusals of ``rebalance_budgets``, at preview: every id in the org, one
# period and a zero net. A row no longer at its ``expected_amount`` is
# disclosed at its current amount with a warning; the handler refuses it.

async def _rebalance_preview(ctx: ToolContext, args) -> Preview:
    ids = [i.budget_id for i in args.items]
    rows = {b.id: b for b in (await ctx.db.execute(
        select(Budget).where(Budget.id.in_(ids), Budget.org_id == ctx.org_id)
        .order_by(Budget.id).with_for_update().execution_options(populate_existing=True)
    )).scalars()}
    if len(rows) != len(ids):
        raise NotFoundError("Budget")
    if len({b.period_start for b in rows.values()}) != 1:
        raise ValidationError("All budgets must be in the same period.")
    if sum(i.amount for i in args.items) != sum(i.expected_amount for i in args.items):
        raise ValidationError("Changes must net to zero.")
    currency = await _currency(ctx)
    changes = [
        Change("budgets", i.budget_id, "amount", f"{rows[i.budget_id].amount:.2f}", f"{i.amount:.2f}", currency)
        for i in args.items if i.amount != rows[i.budget_id].amount
    ]
    if not changes:
        raise ToolError("no_change", "the budgets already have these amounts")
    # Staged, not refused: a revert after drift must stage (TBD-589). The
    # handler still refuses the stale rows at confirm, writing nothing.
    stale = [i.budget_id for i in args.items if rows[i.budget_id].amount != i.expected_amount]
    return Preview(
        summary=f"Move allocation between {len(changes)} budgets",
        changes=changes,
        warnings=[
            f"Budget(s) {', '.join(map(str, stale))} changed since: confirming is refused "
            "unless their amounts are back at expected_amount."
        ] if stale else [],
        context={
            "period_start": next(iter(rows.values())).period_start.isoformat(),
            "categories": await _names(ctx, {rows[c.id].category_id for c in changes}),
        },
    )


def _rebalance_inverse(args_json: dict, preview_json: dict) -> dict:
    return {"items": [
        {"budget_id": c["id"], "expected_amount": c["after"], "amount": c["before"]}
        for c in preview_json["changes"]
    ]}


register(route_tool(
    name="budgets_rebalance", risk="write",
    router=ROUTER, method="POST", path="/api/v1/budgets/rebalance",
    fields=("items",), required=("items",),
    product_area=BUDGETS, min_role=Role.MEMBER,
    description=(
        "Move allocation between budgets of one billing period. Each item names a budget, "
        "its current amount (expected_amount, as budgets_list shows it) and its new amount; "
        f"the new amounts must add up to the current ones. {WRITE_CONFIRM}"
    ),
    preview=_rebalance_preview, inverse=_rebalance_inverse,
))


# ── budgets_delete ────────────────────────────────────────────────────────

async def _no_cascade(ctx: ToolContext, row, args) -> list[Change]:
    return []  # nothing references a budget


async def _delete_present(ctx: ToolContext, budget: Budget, args, changes: list[Change]) -> Preview:
    currency = await _currency(ctx)
    changes = [
        Change(c.entity, c.id, c.field, c.before, c.after, currency) if c.field == "amount" else c
        for c in changes
    ]
    name = await ctx.db.scalar(select(Category.name).where(Category.id == budget.category_id))
    return Preview(
        summary=f"Delete budget {budget.id}", changes=changes,
        warnings=["A delete cannot be undone."],
        context={"category_name": name, "period_start": budget.period_start.isoformat()},
    )


register(route_tool(
    name="budgets_delete", risk="sensitive",
    router=ROUTER, method="DELETE", path="/api/v1/budgets/{budget_id}",
    fields=(), product_area=BUDGETS, min_role=Role.MEMBER,
    description=f"Delete one budget. Its category and transactions are kept. {CONFIRM}",
    entity=Budget, describe=("category_id", "amount", "period_start"), present=_delete_present,
    cascade=CascadeDisclosure(covers=frozenset(), disclose=_no_cascade),
))
