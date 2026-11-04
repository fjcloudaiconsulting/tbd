"""The category tools (TBD-666): create, update, move, batch-move, delete and
restore the recommended categories.

Each is route-bound (``app.agent.route_tool``): execute runs the REST handler,
whose audit row records no client address. ``categories_update`` is a
``write`` (a revert renames it back); the others create, re-parent or delete
rows and are ``sensitive``: the person confirms them in the app.

A move is live-reference: only the subcategory's ``parent_id`` changes, and its
transactions, recurring items and forecast items now count under the new
master. A delete renders ``category_service.plan_category_delete``, the
decision the delete applies, so the preview is the delete itself: every row
that moves to the target, the forecast items that fold into the target's, the
rules re-pointed or dropped, and the budgets deleted with it.

Refusal details never carry a category name (user text): the service encodes
names into its conflict strings, so those map to fixed messages here.
"""
from __future__ import annotations

from sqlalchemy import select

from app.agent.registry import Change, Preview, ToolContext, ToolError, register
from app.agent.route_tool import CascadeDisclosure, route_tool
from app.agent.sensitive_tools import CONFIRM
from app.models import Budget, Category, Transaction
from app.models.category import CategoryType
from app.models.recurring import RecurringTransaction
from app.models.scenario import PlanGoal, Scenario
from app.models.user import Role
from app.routers import categories as categories_router
from app.services import category_service, org_bootstrap_service
from app.services.exceptions import ConflictError, ValidationError

ROUTER = categories_router.router
WRITE_CONFIRM = "Returns a preview to confirm; nothing changes until it is confirmed."
COLLISION = "a subcategory with the same name is already under that master"


def _u(text: str | None) -> dict | None:
    return None if text is None else {"untrusted": text}


async def _names(ctx: ToolContext, ids) -> dict[int, str]:
    return dict((await ctx.db.execute(
        select(Category.id, Category.name)
        .where(Category.id.in_([i for i in ids if i is not None]), Category.org_id == ctx.org_id)
    )).all())


# ── categories_create ─────────────────────────────────────────────────────

async def _create_check(ctx: ToolContext, _row, args) -> None:
    if args.parent_id is None:
        return
    parent = await ctx.db.scalar(
        select(Category).where(Category.id == args.parent_id, Category.org_id == ctx.org_id)
    )
    if parent is None:
        raise ValidationError("Invalid parent category")
    if parent.parent_id is not None:
        raise ValidationError("Cannot nest more than two levels")


async def _create_present(ctx: ToolContext, _row, args, changes: list[Change]) -> Preview:
    parent = None
    if args.parent_id is not None:
        parent = await ctx.db.scalar(select(Category).where(Category.id == args.parent_id))
        # A subcategory takes its master's type, whatever the body says.
        changes = [
            Change(c.entity, c.id, c.field, None, parent.type.value) if c.field == "type" else c
            for c in changes
        ]
    return Preview(
        summary="Create a subcategory" if parent else "Create a category",
        changes=changes,
        context={"name": args.name, **({"parent_name": parent.name} if parent else {})},
    )


register(route_tool(
    name="categories_create", risk="sensitive",
    router=ROUTER, method="POST", path="/api/v1/categories",
    fields=("name", "type", "parent_id", "description"), required=("name",),
    product_area=None, min_role=Role.MEMBER,
    description=(
        "Create a category: a master (type income, expense or both) or, with parent_id, a "
        f"subcategory of a master, which takes the master's type. {CONFIRM}"
    ),
    entity=Category, check=_create_check, present=_create_present,
))


# ── categories_update ─────────────────────────────────────────────────────

async def _update_check(ctx: ToolContext, cat: Category, args) -> None:
    if "type" not in args.model_fields_set or CategoryType(args.type) == cat.type:
        return
    new_type = CategoryType(args.type)
    if cat.parent_id is not None:
        parent_type = await ctx.db.scalar(select(Category.type).where(Category.id == cat.parent_id))
        if new_type != parent_type:
            raise ValidationError(
                "Subcategory type must match its parent. Update the master category instead."
            )
    await category_service.validate_category_type_change(ctx.db, cat, new_type)
    try:
        await category_service.assert_min_floor_after_type_change(
            ctx.db, org_id=ctx.org_id, category=cat, new_type=new_type,
        )
    except ConflictError:
        raise ToolError(
            "conflict", "the organization needs at least one income and one expense category "
            "and subcategory; this type change would leave none",
        ) from None


async def _update_present(ctx: ToolContext, cat: Category, args, changes: list[Change]) -> Preview:
    cascade: list[Change] = []
    if cat.parent_id is None and any(c.field == "type" for c in changes):
        # A master's subcategories take its new type.
        children = (await ctx.db.execute(
            select(Category.id, Category.type)
            .where(Category.parent_id == cat.id, Category.org_id == ctx.org_id)
            .order_by(Category.id)
        )).all()
        cascade = [Change("categories", i, "type", t.value, args.type) for i, t in children if t.value != args.type]
    return Preview(
        summary=f"Change {', '.join(c.field for c in changes)} of category {cat.id}"
        + (f" and the type of its {len(cascade)} subcategories" if cascade else ""),
        changes=changes + cascade,
        context={"category_name": cat.name},
    )


register(route_tool(
    name="categories_update", risk="write",
    router=ROUTER, method="PUT", path="/api/v1/categories/{category_id}",
    fields=("name", "type", "description"), nullable=("description",),
    product_area=None, min_role=Role.MEMBER,
    description=(
        "Rename a category, change its description (null clears it) or its type. A master's "
        "subcategories take its new type; a subcategory's type follows its master's. "
        f"{WRITE_CONFIRM}"
    ),
    entity=Category, check=_update_check, present=_update_present,
    no_change="the category already has these values",
))


# ── categories_move / categories_batch_move ───────────────────────────────

async def _resolve_moves(ctx: ToolContext, moves) -> list[Change]:
    """One ``parent_id`` change per move, refused as the service refuses it."""
    changes: list[Change] = []
    seen: set = set()
    for sub_id, target_id in moves:
        try:
            sub, source, target = await category_service._resolve_for_move(
                ctx.db, org_id=ctx.org_id, subcategory_id=sub_id, target_parent_id=target_id,
            )
        except ConflictError:
            raise ToolError("conflict", COLLISION) from None
        key = (target.id, category_service.normalize_category_name(sub.name))
        if sub.id in {c.id for c in changes}:
            raise ValidationError("a subcategory appears in more than one move")
        if key in seen:
            raise ToolError("conflict", COLLISION)
        seen.add(key)
        changes.append(Change("categories", sub.id, "parent_id", source.id, target.id))
    return changes


LIVE = (
    "A move keeps the subcategory's transactions, recurring items and forecast items on it: "
    "they now count under the new master, in budgets and reports too."
)


async def _moved(ctx: ToolContext, changes: list[Change], summary: str) -> Preview:
    names = await _names(ctx, {i for c in changes for i in (c.id, c.before, c.after)})
    return Preview(
        summary=summary, changes=changes, warnings=[LIVE],
        context={"moves": [
            {"category_name": names.get(c.id), "from": {"category_name": names.get(c.before)},
             "to": {"category_name": names.get(c.after)}}
            for c in changes
        ]},
    )


async def _move_preview(ctx: ToolContext, args) -> Preview:
    changes = await _resolve_moves(ctx, [(args.category_id, args.target_parent_id)])
    (c,) = changes
    return await _moved(ctx, changes, f"Move subcategory {c.id} from category {c.before} to category {c.after}")


register(route_tool(
    name="categories_move", risk="sensitive",
    router=ROUTER, method="PATCH", path="/api/v1/categories/{category_id}/move",
    fields=("target_parent_id",), required=("target_parent_id",),
    product_area=None, min_role=Role.MEMBER,
    description=(
        "Move one subcategory under another master of the same type. Its transactions stay "
        f"on it and now count under the new master. {CONFIRM}"
    ),
    preview=_move_preview,
))


async def _batch_move_preview(ctx: ToolContext, args) -> Preview:
    changes = await _resolve_moves(ctx, [(m.subcategory_id, m.target_parent_id) for m in args.moves])
    return await _moved(ctx, changes, f"Move {len(changes)} subcategories")


register(route_tool(
    name="categories_batch_move", risk="sensitive",
    router=ROUTER, method="POST", path="/api/v1/categories/batch-move",
    fields=("moves",), required=("moves",),
    product_area=None, min_role=Role.MEMBER,
    description=(
        "Move several subcategories, each under another master of the same type, all or "
        f"none. Their transactions stay on them and now count under the new masters. {CONFIRM}"
    ),
    preview=_batch_move_preview,
))


# ── categories_delete ─────────────────────────────────────────────────────

async def _plan(ctx: ToolContext, cat: Category, args):
    try:
        return await category_service.plan_category_delete(
            ctx.db, org_id=ctx.org_id, category_id=cat.id, target_category_id=args.target_category_id,
        )
    except ConflictError as exc:
        if exc.detail.startswith("has_children"):
            raise ToolError("conflict", "the category has subcategories: move or delete them first") from None
        raise ToolError(
            "conflict", "the organization needs at least one income and one expense category "
            "and subcategory; deleting this one would leave none",
        ) from None
    except ValidationError as exc:
        if exc.detail.startswith("migration_target_required"):
            raise ToolError(
                "invalid_arguments", "the category is in use: pass target_category_id, the category "
                "its transactions, recurring items, forecast items and plans move to",
            ) from None
        if exc.detail.startswith("type_mismatch"):
            raise ValidationError("the target category's type does not fit the rows that move to it") from None
        raise


async def _delete_cascade(ctx: ToolContext, cat: Category, args) -> list[Change]:
    plan = await _plan(ctx, cat, args)
    out: list[Change] = []
    if plan.target is not None:
        to = plan.target.id
        for model in (Transaction, RecurringTransaction):
            ids = (await ctx.db.scalars(
                select(model.id).where(model.org_id == ctx.org_id, model.category_id == cat.id)
                .order_by(model.id)
            )).all()
            out += [Change(model.__tablename__, i, "category_id", cat.id, to) for i in ids]
        out += [Change("forecast_plan_items", i.id, "category_id", cat.id, to) for i in plan.item_moves]
        for item, into in plan.item_merges:
            out += [
                Change("forecast_plan_items", into.id, "planned_amount", f"{into.planned_amount:.2f}",
                       f"{into.planned_amount + item.planned_amount:.2f}"),
                Change("forecast_plan_items", item.id, "category_id", cat.id, None),
                Change("forecast_plan_items", item.id, "planned_amount", f"{item.planned_amount:.2f}", None),
            ]
        # TBD-601: plans and plan goals on it move too.
        for model in (Scenario, PlanGoal):
            ids = (await ctx.db.scalars(
                select(model.id).where(model.org_id == ctx.org_id, model.category_id == cat.id)
                .order_by(model.id)
            )).all()
            out += [Change(model.__tablename__, i, "category_id", cat.id, to) for i in ids]
        out += [Change("category_rules", _u(r.normalized_token), "category_id", cat.id, to)
                for r in plan.rules_to_migrate]
    out += [Change("category_rules", _u(r.normalized_token), "category_id", cat.id, None)
            for r in plan.rules_to_drop]
    for b in (await ctx.db.scalars(
        select(Budget).where(Budget.org_id == ctx.org_id, Budget.category_id == cat.id).order_by(Budget.id)
    )).all():
        out += [Change("budgets", b.id, "amount", f"{b.amount:.2f}", None),
                Change("budgets", b.id, "period_start", b.period_start.isoformat(), None)]
    return out


async def _delete_present(ctx: ToolContext, cat: Category, args, changes: list[Change]) -> Preview:
    unlinked = ("categories", "scenarios", "plan_goals")  # SET NULL: the row stays
    gone = {(c.entity, str(c.id)): c.id for c in changes if c.after is None and c.entity not in unlinked}
    moved = sum(1 for c in changes if c.entity in ("transactions", "recurring_transactions"))
    plans = sum(1 for c in changes if c.entity in ("scenarios", "plan_goals") and c.after is not None)
    warnings = ["A delete cannot be undone."]
    target = args.target_category_id if any(c.after is not None for c in changes) else None
    if target is not None:
        warnings.append(
            f"{moved} transaction(s) and recurring item(s), its forecast items and its "
            f"categorization rules move to category {target}."
            + (f" {plans} plan(s) and goal(s) on it move too." if plans else "")
        )
    if any(e == "budgets" for e, _ in gone):
        warnings.append("Its budgets are deleted with it.")
    names = await _names(ctx, {target})
    return Preview(
        summary=f"Delete category {cat.id}" + (f", moving what uses it to category {target}" if target else ""),
        changes=changes, warnings=warnings,
        context={
            "category_name": cat.name,
            **({"to": {"category_name": names.get(target)}} if target else {}),
            "deleted": [{"entity": "categories", "id": cat.id}]
            + [{"entity": e, "id": i} for (e, _), i in gone.items()],
        },
    )


register(route_tool(
    name="categories_delete", risk="sensitive",
    router=ROUTER, method="DELETE", path="/api/v1/categories/{category_id}",
    fields=("target_category_id",), product_area=None, min_role=Role.MEMBER,
    description=(
        "Delete a category. A master with subcategories is refused. If transactions, "
        "recurring items or forecast items use it, target_category_id names the category they "
        "move to (its categorization rules move too); its budgets are deleted. Cannot be "
        f"undone. {CONFIRM}"
    ),
    entity=Category, describe=("name", "type", "parent_id"), present=_delete_present,
    cascade=CascadeDisclosure(
        covers=frozenset({
            "budgets.category_id", "categories.parent_id", "category_rules.category_id",
            "forecast_plan_items.category_id", "recurring_transactions.category_id",
            "transactions.category_id", "plan_goals.category_id", "scenarios.category_id",
        }),
        disclose=_delete_cascade,
    ),
))


# ── categories_restore_recommended ────────────────────────────────────────

async def _restore_preview(ctx: ToolContext, args) -> Preview:
    plan = await org_bootstrap_service.plan_recommended_categories(ctx.db, org_id=ctx.org_id)
    if not plan:
        raise ToolError("no_change", "every recommended category is already there")
    changes: list[Change] = []
    for k, row in enumerate(plan, 1):
        parent = row["parent_id"] if row["parent"] is None else f"new:{row['parent'] + 1}"
        changes += [
            Change("categories", f"new:{k}", f, None, v)
            for f, v in (
                ("name", _u(row["name"])), ("slug", row["slug"]), ("description", _u(row["description"])),
                ("type", row["type"].value), ("is_system", True), ("parent_id", parent),
            )
            if v is not None
        ]
    return Preview(summary=f"Restore {len(plan)} recommended categories", changes=changes)


register(route_tool(
    name="categories_restore_recommended", risk="sensitive",
    router=ROUTER, method="POST", path="/api/v1/categories/restore-recommended",
    fields=(), product_area=None, min_role=Role.OWNER,
    description=(
        "Add back the recommended system categories the organization is missing. Existing "
        f"categories are never changed. Owner only. {CONFIRM}"
    ),
    preview=_restore_preview,
))
