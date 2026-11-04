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

from app.agent.registry import Change, Preview, ToolContext, ToolError, ToolSpec, register
from app.agent.route_tool import CascadeDisclosure, refusals, route_tool
from app.agent.sensitive_tools import CONFIRM
from app.models.scenario import PlanGoal, Scenario, ScenarioType
from app.models.user import Role
from app.routers import scenarios as scenarios_router
from app.schemas.scenario import CompareRequest, ScenarioCreate, SimulateRequest
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

register(route_tool(
    name="scenarios_list", risk="read", router=ROUTER, method="GET", path="/api/v1/scenarios",
    fields=("include_archived",), product_area=PLANS, min_role=Role.MEMBER,
    description="The user's plans (what-if scenarios) with params and last projection.",
))
register(route_tool(
    name="scenarios_get", risk="read", router=ROUTER, method="GET", path="/api/v1/scenarios/{scenario_id}",
    fields=(), product_area=PLANS, min_role=Role.MEMBER, description="One of the user's plans.",
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
    run=_simulate,
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
    run=_compare,
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


async def _create_preview(ctx: ToolContext, args) -> Preview:
    body = ScenarioCreate.model_validate(args.model_dump(exclude_unset=True))
    params = body.params.model_dump(mode="json")
    await _check_events(ctx, body.scenario_type.value, body.horizon_months, params)
    values = {"user_id": ctx.user.id, "name": body.name, "scenario_type": body.scenario_type.value,
              "params_json": params, "horizon_months": body.horizon_months, "is_active": True}
    return Preview(
        summary=f"Create a {body.scenario_type.value} plan over {body.horizon_months} months",
        changes=[_change("new:1", f, None, v) for f, v in values.items()],
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
    changes = [c for c in changes if c.before != c.after]
    if not changes:
        raise ToolError("no_change", "the plan already has these values")
    return Preview(summary=f"Change {', '.join(c.field for c in changes)} of plan {row.id}", changes=changes)


register(route_tool(
    name="scenarios_create", risk="sensitive", router=ROUTER, method="POST", path="/api/v1/scenarios",
    fields=("name", "scenario_type", "params", "horizon_months"), required=("name", "scenario_type", "params"),
    lean=("params",), product_area=PLANS, min_role=Role.MEMBER,
    description=f"Create a plan (a what-if scenario; horizon_months default 24). {PARAMS} {CONFIRM}",
    preview=_create_preview,
))
register(route_tool(
    name="scenarios_update", risk="sensitive", router=ROUTER, method="PATCH",
    path="/api/v1/scenarios/{scenario_id}",
    fields=("name", "params", "horizon_months", "is_active"), lean=("params",),
    product_area=PLANS, min_role=Role.MEMBER,
    description=(f"Change a plan's name, params (whole object, as for scenarios_create, same "
                 f"scenario_type), horizon or archive flag (is_active). {CONFIRM}"),
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
