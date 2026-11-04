"""The Plans tools (TBD-668; ``scenarios`` internally): list, read, simulate
and compare plans; create, edit and delete them.

Create, edit and delete are route-bound (``app.agent.route_tool``): execute
runs the REST handler, and every preview applies the route's own ownership
helper (``_get_owned_scenario``), so a plan the route would not show the user
is refused before anything about it is disclosed. All three are ``sensitive``:
a plan's params carry amounts and the accounts that fund them, and a delete
removes its goals.

Simulate and compare are read tools over the two ``READ_POSTS`` routes. Compare
runs the REST handler (it writes nothing). The simulate route caches the
projection on the row; the tool runs the same analytic engine and returns the
projection without writing it. ``ai_enhanced`` is not offered: it calls an AI
provider, which no agent tool reaches.
"""
from __future__ import annotations

import json
from typing import Any, Literal

from fastapi.encoders import jsonable_encoder
from pydantic import ConfigDict
from sqlalchemy import select

from app.agent.registry import Change, Preview, ToolContext, ToolError, ToolSpec, register, untrusted_leaves
from app.agent.route_tool import CascadeDisclosure, _disclose, refusals, route_tool
from app.agent.sensitive_tools import CONFIRM
from app.models.scenario import PlanGoal, Scenario, ScenarioType
from app.models.user import Role
from app.routers import plan_goals as plan_goals_router
from app.routers import scenarios as scenarios_router
from app.schemas.plan_goal import PlanGoalCreate, PlanGoalUpdate
from app.schemas.scenario import PLAN_FIELDS, CompareRequest, ScenarioCreate, ScenarioUpdate, SimulateRequest
from app.services import plan_draft_service
from app.services.exceptions import ValidationError
from app.services.feature_gate import Feature
from app.services.scenario_engine import SimulationRequest, build_world_state, get_engine, simulate_off_loop

ROUTER = scenarios_router.router
PLANS = Feature.PLANS
R = scenarios_router  # the route's own helpers


async def _owned(ctx: ToolContext, scenario_id: int, *, lock: bool = True) -> Scenario:
    """The route's ownership check, then (for a preview) the row read fresh and
    locked. A read takes no lock: its session lives on across model rounds."""
    with refusals():
        row = await R._get_owned_scenario(ctx.db, user=ctx.user, scenario_id=scenario_id)
    if not lock:
        return row
    return await ctx.db.scalar(
        select(Scenario).where(Scenario.id == scenario_id, Scenario.org_id == ctx.org_id)
        .with_for_update().execution_options(populate_existing=True)
    )


# ── reads ─────────────────────────────────────────────────────────────────

def _ai_untrusted(out: Any) -> Any:
    """A plan read whose cached ``projection_json.ai_assumptions`` (a model's
    summary and reasons, and the values it proposed) arrives as untrusted."""
    for plan in out if isinstance(out, list) else [out]:
        projection = plan.get("projection_json")
        if isinstance(projection, dict) and "ai_assumptions" in projection:
            projection["ai_assumptions"] = untrusted_leaves(projection["ai_assumptions"])
    return out


register(route_tool(
    name="scenarios_list", risk="read", router=ROUTER, method="GET", path="/api/v1/scenarios",
    fields=("include_archived",), product_area=PLANS, min_role=Role.MEMBER, result=_ai_untrusted,
    description="The organization's plans (what-if scenarios) with params and last projection.",
))
register(route_tool(
    name="scenarios_get", risk="read", router=ROUTER, method="GET", path="/api/v1/scenarios/{scenario_id}",
    fields=(), product_area=PLANS, min_role=Role.MEMBER, result=_ai_untrusted,
    description="One of the organization's plans.",
))


class SimulateArgs(SimulateRequest):
    model_config = ConfigDict(extra="forbid")
    scenario_id: int
    engine: Literal["analytic"] = "analytic"


async def _simulate(ctx: ToolContext, args: SimulateArgs) -> dict:
    row = await _owned(ctx, args.scenario_id, lock=False)
    horizon = args.horizon_months if args.horizon_months is not None else row.horizon_months
    with refusals():
        R._validate_horizon_or_422(row.scenario_type.value, horizon)
    state = await build_world_state(ctx.db, org_id=ctx.org_id, user_id=ctx.user.id)
    result = await simulate_off_loop(
        get_engine("analytic"),
        SimulationRequest(scenario=row, state=state, horizon_months=horizon, options=args.options),
        smooth_with_regression=args.smooth_with_regression,
    )
    return {"scenario_id": row.id, "name": row.name, "projection": jsonable_encoder(result)}


register(ToolSpec(
    name="scenarios_simulate", risk="read", args=SimulateArgs, product_area=PLANS, min_role=Role.MEMBER,
    mirrors_route=("POST", "/api/v1/scenarios/{scenario_id}/simulate"),
    description="Project a plan's account balances month by month (saves nothing).",
    run=_simulate, route_limit=R.simulate_scenario,
))


class CompareArgs(CompareRequest):
    model_config = ConfigDict(extra="forbid")


async def _compare(ctx: ToolContext, args: CompareArgs) -> dict:
    body = CompareRequest.model_validate(args.model_dump())
    with refusals():  # the handler past its rate-limit decorator: it writes nothing
        out = await R.compare_scenarios.__wrapped__(request=None, body=body, current_user=ctx.user, db=ctx.db)
    return out.model_dump(mode="json")


register(ToolSpec(
    name="scenarios_compare", risk="read", args=CompareArgs, product_area=PLANS, min_role=Role.MEMBER,
    mirrors_route=("POST", "/api/v1/scenarios/compare"),
    description="Project 1 to 3 plans at one horizon, side by side.",
    run=_compare, route_limit=R.compare_scenarios,
))


# ── create, update, delete ────────────────────────────────────────────────

PARAMS = (
    "params: {scenario_type, ...} with that type's fields: trip {destination, start_date, duration_days, "
    "currency, transport_cost, accommodation_per_night, daily_budget, source_account_id, one_off_extras "
    "[{label, amount, on_date}]}; purchase {subtype, label, target_date, currency, total_price, "
    "down_payment, down_payment_account_id, financing? {principal, annual_rate_pct, term_months, "
    "first_payment_date, payment_account_id}}; retirement {target_retirement_date, currency, "
    "monthly_contribution, contribution_account_id, target_balance, annual_return_pct, inflation_pct?, "
    "contribution_curve? [{from, monthly}]}; custom {label, events [{type: income_off|expense_off|"
    "recurring_on|one_off_income|one_off_expense, month or from_month/to_month, ...}]}."
)


PLAN = (
    "Plan fields (optional): currency (must be the organization's), monthly_budget, birth_year, "
    "return_pct, inflation_pct (yearly %), and funding_account_id, category_id, tag_id in this "
    "organization (the account in the plan's currency)."
)


def _change(sid, field: str, before: Any, after: Any) -> Change:
    """User text (the name, and the params document as its canonical JSON) is disclosed wrapped."""
    def wrap(v):
        if v is None or field not in ("name", "params_json"):
            return v
        return {"untrusted": v if field == "name" else json.dumps(v, sort_keys=True)}
    return Change("scenarios", sid, field, wrap(before), wrap(after))


async def _check_events(ctx: ToolContext, scenario_type: str, horizon: int, params: dict) -> None:
    if scenario_type == ScenarioType.CUSTOM.value:
        with refusals():
            await R._validate_custom_event_references(
                ctx.db, org_id=ctx.org_id, horizon_months=horizon, params=params)


_COLUMNS = Scenario.__table__.c


async def _plan_fields(ctx: ToolContext, row: Scenario, sent: dict[str, Any], *, creating: bool) -> dict:
    """The plan fields the route would store (TBD-601): its own
    ``apply_plan_fields`` (currency scope, links in the org, a draft only) on
    a transient copy of ``row``, so the preview writes nothing."""
    copy = Scenario(**{c.key: getattr(row, c.key) for c in _COLUMNS if c.key in (*PLAN_FIELDS, "org_id", "status")})
    with refusals():
        await plan_draft_service.apply_plan_fields(ctx.db, copy, sent, creating=creating)
    return {f: _disclose(_COLUMNS[f], getattr(copy, f)) for f in PLAN_FIELDS}


async def _create_preview(ctx: ToolContext, args) -> Preview:
    body = ScenarioCreate.model_validate(args.model_dump(exclude_unset=True))
    params = body.params.model_dump(mode="json")
    await _check_events(ctx, body.scenario_type.value, body.horizon_months, params)
    plan = await _plan_fields(ctx, Scenario(org_id=ctx.org_id), body.plan_changes(), creating=True)
    values = {"user_id": ctx.user.id, "name": body.name, "scenario_type": body.scenario_type.value,
              "params_json": params, "horizon_months": body.horizon_months, "is_active": True}
    changes = [_change("new:1", f, None, v) for f, v in values.items()]
    changes += [Change("scenarios", "new:1", f, None, v) for f, v in plan.items() if v is not None]
    return Preview(
        summary=f"Create a {body.scenario_type.value} plan over {body.horizon_months} months",
        changes=changes, context={"currency": plan["currency"]},
    )


async def _update_preview(ctx: ToolContext, args) -> Preview:
    row = await _owned(ctx, args.scenario_id)
    sent = args.model_fields_set
    horizon = row.horizon_months
    changes = []
    if "horizon_months" in sent:
        with refusals():
            R._validate_horizon_or_422(row.scenario_type.value, args.horizon_months)
        horizon = args.horizon_months
        changes.append(_change(row.id, "horizon_months", row.horizon_months, horizon))
    if "name" in sent:
        changes.append(_change(row.id, "name", row.name, args.name))
    if "params" in sent:
        if args.params["scenario_type"] != row.scenario_type.value:
            raise ValidationError("params scenario_type must match the existing scenario_type on the row")
        await _check_events(ctx, row.scenario_type.value, horizon, args.params)
        changes.append(_change(row.id, "params_json", row.params_json, args.params))
    if "is_active" in sent:
        changes.append(_change(row.id, "is_active", row.is_active, args.is_active))
    body = ScenarioUpdate.model_validate(args.model_dump(include=sent - {"scenario_id"}))
    if body.plan_changes():
        after = await _plan_fields(ctx, row, body.plan_changes(), creating=False)
        changes += [Change("scenarios", row.id, f, _disclose(_COLUMNS[f], getattr(row, f)), v)
                    for f, v in after.items()]
    changes = [c for c in changes if c.before != c.after]
    if not changes:
        raise ToolError("no_change", "the plan already has these values")
    return Preview(summary=f"Change {', '.join(c.field for c in changes)} of plan {row.id}", changes=changes)


register(route_tool(
    name="scenarios_create", risk="sensitive", router=ROUTER, method="POST", path="/api/v1/scenarios",
    fields=("name", "scenario_type", "params", "horizon_months", *PLAN_FIELDS),
    required=("name", "scenario_type", "params"),
    lean=("params",), product_area=PLANS, min_role=Role.MEMBER,
    description=f"Create a plan (a what-if scenario; horizon_months default 24). {PARAMS} {PLAN} {CONFIRM}",
    preview=_create_preview,
))
register(route_tool(
    name="scenarios_update", risk="sensitive", router=ROUTER, method="PATCH",
    path="/api/v1/scenarios/{scenario_id}",
    fields=("name", "params", "horizon_months", "is_active", *PLAN_FIELDS), lean=("params",),
    nullable=("currency", "monthly_budget", "birth_year", "funding_account_id", "category_id", "tag_id"),
    product_area=PLANS, min_role=Role.MEMBER,
    description=(f"Change a plan's name, params (whole object, as for scenarios_create, same "
                 f"scenario_type), horizon, archive flag (is_active) or plan fields (a draft only; "
                 f"null clears one). {PLAN} {CONFIRM}"),
    preview=_update_preview,
))


async def _owner_check(ctx: ToolContext, row: Scenario, args) -> None:
    with refusals():
        await R._get_owned_scenario(ctx.db, user=ctx.user, scenario_id=row.id)


async def _goals(ctx: ToolContext, row: Scenario, args) -> list[Change]:
    goals = (await ctx.db.scalars(
        select(PlanGoal).where(PlanGoal.scenario_id == row.id).order_by(PlanGoal.id)
    )).all()
    return [Change("plan_goals", g.id, "name", {"untrusted": g.name}, None) for g in goals]


async def _delete_present(ctx: ToolContext, row: Scenario, args, changes: list[Change]) -> Preview:
    return Preview(
        summary=f"Delete plan {row.id} and its goals", changes=changes,
        warnings=["A delete cannot be undone."],
        context={"deleted": [{"entity": c.entity, "id": c.id} for c in changes if c.entity == "plan_goals"]},
    )


register(route_tool(
    name="scenarios_delete", risk="sensitive", router=ROUTER, method="DELETE",
    path="/api/v1/scenarios/{scenario_id}", fields=(), product_area=PLANS, min_role=Role.MEMBER,
    description=f"Delete a plan and its goals (to archive instead, scenarios_update is_active=false). {CONFIRM}",
    entity=Scenario, describe=("name", "scenario_type"), check=_owner_check, present=_delete_present,
    cascade=CascadeDisclosure(covers=frozenset({"plan_goals.scenario_id"}), disclose=_goals),
))


# ── plan goals (TBD-601 routes) ───────────────────────────────────────────
#
# The route's own plan lookup and the service's checks, read-only: the plan
# in the caller's org, a draft only, the currency scope and every link in the
# org. ``create_goal`` / ``update_goal`` / ``delete_goal`` write; the previews
# run their guards and name the row instead.

GOALS = "plan_goals"
_GOAL_COLUMNS = PlanGoal.__table__.c
GOAL_FIELDS = (
    "name", "tier", "kind", "target_amount", "target_date", "monthly_amount", "enabled", "sort_order",
    "category_id", "account_id", "start_balance", "funding_recurring_id",
)
GOAL_NULLABLE = ("target_amount", "target_date", "monthly_amount", "category_id", "account_id",
                 "start_balance", "funding_recurring_id")
GOAL_TEXT = (
    "Links must be in this organization, and account_id in the plan's currency."
)


async def _goal_plan(ctx: ToolContext, scenario_id: int, links: dict[str, Any]) -> Scenario:
    with refusals():
        plan = await plan_draft_service.get_plan(ctx.db, org_id=ctx.org_id, plan_id=scenario_id)
        plan_draft_service._assert_draft(plan)
    currency = await plan_draft_service.plan_currency(ctx.db, org_id=plan.org_id, requested=plan.currency)
    await plan_draft_service._check_links(ctx.db, org_id=plan.org_id, currency=currency, changes=links)
    return plan


async def _goal(ctx: ToolContext, plan: Scenario, goal_id: int) -> PlanGoal:
    goal = await plan_draft_service.get_goal(ctx.db, plan=plan, goal_id=goal_id)
    return await ctx.db.scalar(
        select(PlanGoal).where(PlanGoal.id == goal.id).with_for_update().execution_options(populate_existing=True)
    )


def _goal_change(gid, field: str, before: Any, after: Any) -> Change:
    return Change(GOALS, gid, field, _disclose(_GOAL_COLUMNS[field], before), _disclose(_GOAL_COLUMNS[field], after))


async def _goal_create_preview(ctx: ToolContext, args) -> Preview:
    fields = PlanGoalCreate.model_validate(args.model_dump(exclude={"scenario_id"}, exclude_unset=True)).model_dump()
    plan = await _goal_plan(ctx, args.scenario_id, fields)
    changes = [Change(GOALS, "new:1", "scenario_id", None, plan.id)]
    changes += [_goal_change("new:1", f, None, v) for f, v in fields.items() if v is not None]
    return Preview(summary=f"Add a goal to plan {plan.id}", changes=changes)


async def _goal_update_preview(ctx: ToolContext, args) -> Preview:
    sent = PlanGoalUpdate.model_validate(
        args.model_dump(include=args.model_fields_set - {"scenario_id", "goal_id"})).changes()
    plan = await _goal_plan(ctx, args.scenario_id, sent)
    goal = await _goal(ctx, plan, args.goal_id)
    changes = [_goal_change(goal.id, f, getattr(goal, f), v) for f, v in sent.items()]
    changes = [c for c in changes if c.before != c.after]
    if not changes:
        raise ToolError("no_change", "the goal already has these values")
    return Preview(summary=f"Change {', '.join(c.field for c in changes)} of goal {goal.id}", changes=changes)


async def _goal_delete_preview(ctx: ToolContext, args) -> Preview:
    plan = await _goal_plan(ctx, args.scenario_id, {})
    goal = await _goal(ctx, plan, args.goal_id)  # nothing references a goal
    return Preview(
        summary=f"Delete goal {goal.id} of plan {plan.id}",
        changes=[Change(GOALS, goal.id, "name", {"untrusted": goal.name}, None)],
        warnings=["A delete cannot be undone."], context={"deleted": [{"entity": GOALS, "id": goal.id}]},
    )


GOAL_PATH = "/api/v1/scenarios/{scenario_id}/goals"
register(route_tool(
    name="plan_goal_create", risk="sensitive", router=plan_goals_router.router, method="POST", path=GOAL_PATH,
    fields=GOAL_FIELDS, required=("name", "tier", "kind"), product_area=PLANS, min_role=Role.MEMBER,
    description=f"Add a goal to a draft plan. {GOAL_TEXT} {CONFIRM}", preview=_goal_create_preview,
))
register(route_tool(
    name="plan_goal_update", risk="sensitive", router=plan_goals_router.router, method="PATCH",
    path=GOAL_PATH + "/{goal_id}", fields=GOAL_FIELDS, nullable=GOAL_NULLABLE, product_area=PLANS,
    min_role=Role.MEMBER, description=f"Change a goal of a draft plan (null clears an optional field). {CONFIRM}",
    preview=_goal_update_preview,
))
register(route_tool(
    name="plan_goal_delete", risk="sensitive", router=plan_goals_router.router, method="DELETE",
    path=GOAL_PATH + "/{goal_id}", fields=(), product_area=PLANS, min_role=Role.MEMBER,
    description=f"Delete a goal of a draft plan. {CONFIRM}", preview=_goal_delete_preview,
))
