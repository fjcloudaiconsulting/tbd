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

Finish (``plans_finish``, TBD-604) is a hand-written tool over the route's
handler: the route takes a raw ``Request`` for its rate limit.
"""
from __future__ import annotations

import datetime
import json
from types import SimpleNamespace
from typing import Any, Literal

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.agent.recurring_tools import TEMPLATE, _removal
from app.agent.registry import Change, Preview, ToolContext, ToolError, ToolSpec, register, untrusted_leaves
from app.agent.route_tool import CascadeDisclosure, _disclose, refusals, route_tool
from app.agent.sensitive_tools import CONFIRM
from app.models.budget import Budget
from app.models.category import Category
from app.models.recurring import RecurringTransaction
from app.models.scenario import PlanGoal, Scenario, ScenarioStatus, ScenarioType
from app.models.user import Role
from app.routers import plan_goals as plan_goals_router
from app.routers import scenarios as scenarios_router
from app.schemas.plan_goal import PlanGoalCreate, PlanGoalUpdate
from app.schemas.scenario import (
    PLAN_FIELDS, CompareRequest, ScenarioCreate, ScenarioResponse, ScenarioUpdate, SimulateRequest,
)
from app.services import billing_service, plan_draft_service, plan_lifecycle_service, tag_service
from app.services.exceptions import ValidationError
from app.services.feature_gate import Feature
from app.services.scenario_engine import SimulationRequest, build_world_state, get_engine, simulate_off_loop

ROUTER = scenarios_router.router
PLANS = Feature.PLANS
R = scenarios_router  # the route's own helpers


async def _owned(ctx: ToolContext, scenario_id: int, *, lock: bool = True) -> Scenario:
    """The route's visibility check (``not_found``) and, for a preview, its
    edit check (``forbidden``: the creator, or an org owner or admin), then
    the row read fresh and locked. A read takes no lock: its session lives on
    across model rounds."""
    with refusals():
        row = await R._get_owned_scenario(ctx.db, user=ctx.user, scenario_id=scenario_id, write=lock)
    if not lock:
        return row
    # The org's plans locked first, as every plan write does (``plan_lifecycle_service.lock``).
    return await plan_lifecycle_service.lock(ctx.db, row)


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


async def _plan_fields(ctx: ToolContext, row: Scenario, sent: dict[str, Any]) -> dict:
    """The plan fields the route would store (TBD-601): its own
    ``apply_plan_fields`` (currency scope, links in the org) on a transient
    copy of ``row``, so the preview writes nothing. Sending none leaves a
    legacy row as it is; sending any makes the row a plan."""
    copy = Scenario(**{c.key: getattr(row, c.key) for c in _COLUMNS if c.key in (*PLAN_FIELDS, "org_id", "status")})
    with refusals():
        await plan_draft_service.apply_plan_fields(ctx.db, copy, sent)
    return {f: _disclose(_COLUMNS[f], getattr(copy, f)) for f in PLAN_FIELDS}


async def _create_preview(ctx: ToolContext, args) -> Preview:
    body = ScenarioCreate.model_validate(args.model_dump(exclude_unset=True))
    params = body.params.model_dump(mode="json")
    await _check_events(ctx, body.scenario_type.value, body.horizon_months, params)
    plan = await _plan_fields(ctx, Scenario(org_id=ctx.org_id), body.plan_changes())
    values = {"user_id": ctx.user.id, "name": body.name, "scenario_type": body.scenario_type.value,
              "params_json": params, "horizon_months": body.horizon_months, "is_active": True}
    changes = [_change("new:1", f, None, v) for f, v in values.items()]
    changes += [Change("scenarios", "new:1", f, None, v) for f, v in plan.items() if v is not None]
    return Preview(
        summary=f"Create a {body.scenario_type.value} plan over {body.horizon_months} months",
        changes=changes, context={"currency": plan["currency"]} if plan["currency"] else {},
    )


async def _update_preview(ctx: ToolContext, args) -> Preview:
    row = await _owned(ctx, args.scenario_id)
    sent = args.model_fields_set - {"confirm"}
    with refusals(), plan_goals_router.http_errors():
        plan_draft_service.assert_editable(
            row, confirm=args.confirm, changes={k: getattr(args, k) for k in sent - {"scenario_id"}},
            allowed=plan_draft_service.STARTED_PLAN_FIELDS)
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
        after = await _plan_fields(ctx, row, body.plan_changes())
        changes += [Change("scenarios", row.id, f, _disclose(_COLUMNS[f], getattr(row, f)), v)
                    for f, v in after.items()]
    if row.status == ScenarioStatus.ACTIVE and "monthly_budget" in sent:
        changes += await _rebudget(ctx, row, body.monthly_budget)
    changes = [c for c in changes if c.before != c.after]
    if not changes:
        raise ToolError("no_change", "the plan already has these values")
    budgets = {c.id for c in changes if c.entity == "budgets"}
    return Preview(
        summary=f"Change {', '.join(dict.fromkeys(c.field if c.entity == 'scenarios' else 'Budgets' for c in changes))} "
                f"of plan {row.id}",
        changes=changes,
        warnings=[f"{len(budgets)} Budget(s) from the current period on are set to the new amount, "
                  "replacing any hand edit or rebalance of them."] if budgets else [],
    )


async def _rebudget(ctx: ToolContext, row: Scenario, monthly_budget, extra: tuple = (),
                    category_ref: Any = None) -> list[Change]:
    """A started plan's new ``monthly_budget``, or a goal started late
    (``extra``), reaches its Budget in the open period and every later one,
    made where missing (``plan_lifecycle_service.budget_changed``); at Start,
    the Budgets ``_ensure_budgets`` makes, on ``category_ref`` (a master not
    made yet)."""
    period = await billing_service.find_open_period(ctx.db, row.org_id)
    if period is None:
        return []
    # The plan as it will be: only the cap changes (a started plan keeps its category).
    probe = SimpleNamespace(id=row.id, org_id=row.org_id, category_id=row.category_id,
                            monthly_budget=monthly_budget)
    out: list[Change] = []
    for k, (p, budget, amount) in enumerate(await plan_lifecycle_service.budget_plan(
            ctx.db, probe, period.start_date, recompute=True, extra=extra), 1):
        if budget is not None:
            out.append(Change("budgets", budget.id, "amount", f"{budget.amount:.2f}", f"{amount:.2f}", row.currency))
            continue
        gid = f"new:budget:{k}"
        out += [Change("budgets", gid, "category_id", None, category_ref or row.category_id),
                Change("budgets", gid, "amount", None, f"{amount:.2f}", row.currency),
                Change("budgets", gid, "period_start", None, p.start_date.isoformat())]
        if p.end_date is not None:
            out.append(Change("budgets", gid, "period_end", None, p.end_date.isoformat()))
    return out


register(route_tool(
    name="scenarios_create", risk="sensitive", result=_ai_untrusted, router=ROUTER, method="POST", path="/api/v1/scenarios",
    fields=("name", "scenario_type", "params", "horizon_months", *PLAN_FIELDS),
    required=("name", "scenario_type", "params"),
    lean=("params",), product_area=PLANS, min_role=Role.MEMBER,
    description=f"Create a plan (a what-if scenario; horizon_months default 24). {PARAMS} {PLAN} {CONFIRM}",
    preview=_create_preview,
))
register(route_tool(
    name="scenarios_update", risk="sensitive", result=_ai_untrusted, router=ROUTER, method="PATCH",
    path="/api/v1/scenarios/{scenario_id}",
    fields=("name", "params", "horizon_months", "is_active", *PLAN_FIELDS, "confirm"), lean=("params",),
    nullable=("currency", "monthly_budget", "birth_year", "funding_account_id", "category_id", "tag_id"),
    product_area=PLANS, min_role=Role.MEMBER,
    description=(f"Change a plan's name, params (whole object, as for scenarios_create, same "
                 f"scenario_type), horizon, archive flag (is_active) or plan fields (null clears one). "
                 f"A started plan takes only name, params, horizon_months, monthly_budget (not null; "
                 f"its Budgets from the current period on follow), birth_year, return_pct and "
                 f"inflation_pct, and only with confirm=true; a finished plan takes nothing. "
                 f"{PLAN} {CONFIRM}"),
    preview=_update_preview,
))


async def _owner_check(ctx: ToolContext, row: Scenario, args) -> None:
    with refusals():
        await R._get_owned_scenario(ctx.db, user=ctx.user, scenario_id=row.id, write=True)


async def _goals(ctx: ToolContext, row: Scenario, args) -> list[Change]:
    # Takes the locks (``locks=True``): the org's plans first, as every plan write does.
    row = await plan_lifecycle_service.lock(ctx.db, row)
    # ``delete_plan``: an active plan is wound down first; a finished one was at Finish.
    out = await _wind_down(ctx, row) if row.status == ScenarioStatus.ACTIVE else []
    goals = (await ctx.db.scalars(
        select(PlanGoal).where(PlanGoal.scenario_id == row.id).order_by(PlanGoal.id)
    )).all()
    return out + [Change("plan_goals", g.id, "name", {"untrusted": g.name}, None) for g in goals]


async def _delete_present(ctx: ToolContext, row: Scenario, args, changes: list[Change]) -> Preview:
    return Preview(
        summary=f"Delete plan {row.id} and its goals", changes=changes,
        warnings=["A delete cannot be undone.", *_wind_down_warnings(changes)],
        context={"deleted": _gone(changes, "plan_goals")},
    )


register(route_tool(
    name="scenarios_delete", risk="sensitive", router=ROUTER, method="DELETE",
    path="/api/v1/scenarios/{scenario_id}", fields=(), product_area=PLANS, min_role=Role.MEMBER,
    description=(f"Delete a plan and its goals (to archive instead, scenarios_update is_active=false). "
                 f"A started plan is wound down first, as plans_finish does; what Start created "
                 f"(category, tag, past Budgets, transactions) stays. {CONFIRM}"),
    entity=Scenario, describe=("name", "scenario_type"), check=_owner_check, present=_delete_present,
    cascade=CascadeDisclosure(covers=frozenset({"plan_goals.scenario_id"}), disclose=_goals, locks=True),
))


# ── the lifecycle (TBD-602 routes, TBD-604) ───────────────────────────────
#
# Finish and an active plan's Delete wind the plan down
# (``plan_lifecycle_service._wind_down``): each live funding series stopped
# as ``recurring_stop`` stops one (its pending occurrences from today on
# deleted, the rest leaving the series), and the plan's Budgets after the
# open period deleted. Start's preview names every row its steps create,
# from the service's read-only halves (``master_plan``, ``tag_plan``,
# ``goal_steps``, ``budget_plan``; TBD-604).


async def _wind_down(ctx: ToolContext, plan: Scenario) -> list[Change]:
    out: list[Change] = []
    seen: set[int] = set()
    for goal in await plan_draft_service.list_goals(ctx.db, plan=plan):
        series = goal.funding_recurring_id and await ctx.db.get(RecurringTransaction, goal.funding_recurring_id)
        if series and series.org_id == plan.org_id and series.is_active and series.id not in seen:
            seen.add(series.id)
            out.append(Change(TEMPLATE, series.id, "is_active", True, False))
            out += await _removal(ctx, series)
    period = await billing_service.find_open_period(ctx.db, plan.org_id)
    if period is not None and plan.category_id is not None:
        budgets = (await ctx.db.scalars(select(Budget).where(
            Budget.org_id == plan.org_id, Budget.category_id == plan.category_id,
            Budget.period_start > period.start_date).order_by(Budget.id))).all()
        for b in budgets:
            out += [Change("budgets", b.id, "category_id", b.category_id, None),
                    Change("budgets", b.id, "amount", f"{b.amount:.2f}", None, plan.currency),
                    Change("budgets", b.id, "period_start", b.period_start.isoformat(), None)]
    return out


def _gone(changes: list[Change], *entities: str) -> list[dict]:
    """The rows that cease to exist: a deleted transaction is named by its
    ``type``, a budget by its ``period_start``, a tag link by its ``tag_id``."""
    keys = {("transactions", "type"), ("budgets", "period_start"), ("transaction_tags", "tag_id"),
            *((e, "name") for e in entities)}
    return [{"entity": c.entity, "id": c.id} for c in changes
            if (c.entity, c.field) in keys and c.after is None]


def _wind_down_warnings(changes: list[Change]) -> list[str]:
    series = sum(1 for c in changes if c.entity == TEMPLATE)
    pending = sum(1 for c in changes if (c.entity, c.field) == ("transactions", "type"))
    demoted = sum(1 for c in changes if c.field == "reconciliation_state")
    budgets = sum(1 for c in changes if (c.entity, c.field) == ("budgets", "period_start"))
    out = [f"{series} funding series stop; their other transactions leave the series."] if series else []
    if pending:
        out.append(f"{pending} pending transaction(s) from today on are deleted.")
    if demoted:
        out.append(f"{demoted} matched import row(s) pointing at them will be marked rejected, "
                   "which cannot be undone.")
    if budgets:
        out.append(f"{budgets} Budget(s) after the current period are deleted; the current one stays.")
    return out


class FinishArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario_id: int


async def _finish_preview(ctx: ToolContext, args: FinishArgs) -> Preview:
    row = await _owned(ctx, args.scenario_id)
    if row.status == ScenarioStatus.FINISHED:
        raise ToolError("no_change", "the plan is already finished")
    if row.status != ScenarioStatus.ACTIVE:
        raise ToolError("conflict", "only an active plan can be finished")
    # ``finished_at`` is the clock stamp of the transition named here as ``status``.
    changes = [Change("scenarios", row.id, "status", ScenarioStatus.ACTIVE.value, ScenarioStatus.FINISHED.value)]
    changes += await _wind_down(ctx, row)
    return Preview(
        summary=f"Finish plan {row.id}", changes=changes,
        warnings=_wind_down_warnings(changes) + ["A finished plan cannot be started again."],
        context={"name": {"untrusted": row.name}, "deleted": _gone(changes)},
    )


async def _finish_execute(ctx: ToolContext, args: FinishArgs) -> dict:
    with refusals():  # the handler past its rate-limit decorator (``route_limit`` applies it)
        row = await R.finish_plan.__wrapped__(
            scenario_id=args.scenario_id, request=None, current_user=ctx.user, db=ctx.db)
    return _ai_untrusted(ScenarioResponse.model_validate(row).model_dump(mode="json"))


register(ToolSpec(
    name="plans_finish", risk="sensitive", args=FinishArgs, product_area=PLANS, min_role=Role.MEMBER,
    mirrors_route=("POST", "/api/v1/scenarios/{scenario_id}/finish"),
    description=(
        "Finish an active plan: its funding series stop (their pending transactions from today on "
        "are deleted) and its Budgets after the current period are deleted; the current Budget, the "
        f"category, the tag and all history stay. A finished plan cannot be started again. {CONFIRM}"
    ),
    preview=_finish_preview, execute=_finish_execute, route_limit=R.finish_plan,
))


MASTER = "new:category:master"
_SERIES_COLUMNS = RecurringTransaction.__table__.c
NO_PERIOD = "the organization has no open billing period: open one first (billing_period_create)"


class StartArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario_id: int


async def _start_preview(ctx: ToolContext, args: StartArgs) -> Preview:
    """Start's steps as ``plan_lifecycle_service.start`` takes them, from its
    read-only halves (architects' ruling on TBD-604). ``started_at`` and the
    frozen ``baseline_projection_json`` are not named (fence bookkeeping)."""
    row = await _owned(ctx, args.scenario_id)
    if row.status == ScenarioStatus.ACTIVE:
        raise ToolError("no_change", "the plan is already started")
    if row.status != ScenarioStatus.DRAFT:
        raise ToolError("conflict", "a finished plan cannot be started again")
    if not plan_draft_service.is_plan(row):
        raise ToolError("invalid_arguments", f"scenario {row.id} is not a plan: give it plan fields first")
    # ``get_current_period`` would commit a new period outside the plan's locks.
    if await billing_service.find_open_period(ctx.db, row.org_id) is None:
        raise ToolError("no_open_period", NO_PERIOD)
    today = datetime.date.today()
    with refusals(), plan_goals_router.http_errors():
        plan = await plan_lifecycle_service.lock(ctx.db, row)
        _, goals = await plan_lifecycle_service.start_checks(ctx.db, plan)
        master = await plan_lifecycle_service.master_plan(ctx.db, plan, linked_at_start=True)
        tag_id, tag_name = await plan_lifecycle_service.tag_plan(ctx.db, plan)
    changes = [Change("scenarios", plan.id, "status", ScenarioStatus.DRAFT.value, ScenarioStatus.ACTIVE.value)]
    if master.id is None:
        changes += [Change("categories", MASTER, "name", None, {"untrusted": master.name}),
                    Change("categories", MASTER, "type", None, master.type.value),
                    Change("scenarios", plan.id, "category_id", None, MASTER)]
    if tag_name is not None:
        normalized = tag_service.normalize_tag_name(tag_name)
        with refusals():
            await tag_service.ensure_local_tag_changes(ctx.db, plan.org_id, adding=[normalized])
        changes += [Change("tags", "new:tag", "name", None, {"untrusted": tag_name}),
                    Change("tags", "new:tag", "name_normalized", None, {"untrusted": normalized}),
                    Change("tags", "new:tag", "created_by_user_id", None, plan.user_id),
                    Change("scenarios", plan.id, "tag_id", None, "new:tag")]
    elif plan.tag_id is None:
        changes.append(Change("scenarios", plan.id, "tag_id", None, tag_id))
    extra: tuple = ()
    series = 0
    for k, goal in enumerate(goals, 1):
        fields, steps, pair = await _goal_start(ctx, plan, goal, k, today, master)
        changes += [_goal_change(goal.id, f, getattr(goal, f), v) for f, v in fields.items()] + steps
        extra, series = extra + pair, series + bool(pair)
    # No row has id 0: a master not made yet has no Budgets.
    probe = SimpleNamespace(id=plan.id, org_id=plan.org_id, category_id=plan.category_id or 0,
                            currency=plan.currency)
    budgets = await _rebudget(ctx, probe, plan.monthly_budget, extra, MASTER if master.id is None else None)
    changes += budgets
    warnings = ["A started plan cannot go back to draft: Finish stops its series and drops its later "
                "Budgets, but its category, tag, past Budgets and transactions stay."]
    if series:
        warnings.append(f"{series} funding series start, posting pending transactions on their dates.")
    if n := len({c.id for c in budgets}):
        warnings.append(f"{n} Budget(s) are created on the plan's category from the current period on.")
    if master.id is not None or tag_id is not None:
        warnings.append("The plan takes over its linked category and the tag named after it.")
    if any(c.field == "start_balance" for c in changes):
        warnings.append("A goal's starting balance is its account's balance, pending transactions included.")
    return Preview(summary=f"Start plan {plan.id}", changes=changes, warnings=warnings,
                   context={"name": {"untrusted": plan.name},
                            "baseline": "a projection of the plan as drafted is saved as its baseline"})


async def _start_execute(ctx: ToolContext, args: StartArgs) -> dict:
    # The period the preview found may have closed since: never let the route create one.
    if await billing_service.find_open_period(ctx.db, ctx.org_id) is None:
        raise ToolError("no_open_period", NO_PERIOD)
    with refusals():  # the handler past its rate-limit decorator (``route_limit`` applies it)
        row = await R.start_plan.__wrapped__(
            scenario_id=args.scenario_id, request=None, current_user=ctx.user, db=ctx.db)
    return _ai_untrusted(ScenarioResponse.model_validate(row).model_dump(mode="json"))


register(ToolSpec(
    name="plans_start", risk="sensitive", args=StartArgs, product_area=PLANS, min_role=Role.MEMBER,
    mirrors_route=("POST", "/api/v1/scenarios/{scenario_id}/start"),
    description=(
        "Start a draft plan: it gets its master category (made unless linked), its tag, a subcategory "
        "and funding series per enabled goal, its Budgets from the current period on, and a frozen "
        "baseline. Needs an open billing period. A started plan cannot go back to draft. " + CONFIRM
    ),
    preview=_start_preview, execute=_start_execute, route_limit=R.start_plan,
))


# ── plan goals (TBD-601 routes) ───────────────────────────────────────────
#
# The route's own plan lookup and the service's checks, read-only: the plan
# in the caller's org, a draft (or, with confirm, a started plan: TBD-604),
# the currency scope and every link in the org. ``create_goal`` /
# ``update_goal`` / ``delete_goal`` write; the previews run their guards and
# name the row instead, plus what Start's steps write for a goal added or
# enabled on a started plan (``plan_lifecycle_service.goal_steps``).

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


async def _goal_plan(ctx: ToolContext, scenario_id: int, links: dict[str, Any] | None, *,
                     confirm: bool = False, allowed: frozenset[str] | None = None) -> Scenario:
    """As the goal routes: the plan seen (``not_found``) and changeable
    (``forbidden``), locked (the org's plans before the goal: the order every
    plan write takes), and a draft, or (a create or update; ``links`` not
    None) editable after start (``assert_editable``), its currency scope and
    every link in the org."""
    with refusals(), plan_goals_router.http_errors():
        plan = await plan_draft_service.get_plan(ctx.db, user=ctx.user, plan_id=scenario_id, write=True)
        plan = await plan_lifecycle_service.lock(ctx.db, plan)
        if links is None:  # ``delete_goal`` checks the draft only
            plan_draft_service.assert_draft(plan)
            return plan
        plan_draft_service.assert_editable(plan, confirm=confirm, changes=links, allowed=allowed)
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


_SERIES_COLUMNS = RecurringTransaction.__table__.c


async def _goal_start(ctx: ToolContext, plan: Scenario, goal: PlanGoal, k: int, today: datetime.date,
                      master: Category | None = None) -> tuple[dict[str, Any], list[Change], tuple]:
    """One goal's share of Start's steps (``plan_lifecycle_service.goal_steps``):
    the goal fields they set, the subcategory and series they insert (group ids
    ``new:category:<k>``, ``new:series:<k>``), and its (goal, series) pair for
    ``budget_plan``. ``goal`` is the goal as it will be, never the locked row."""
    with refusals(), plan_goals_router.http_errors():
        child, balance, series = await plan_lifecycle_service.goal_steps(ctx.db, plan, goal, today, master=master)
    fields: dict[str, Any] = {}
    changes: list[Change] = []
    if child is not None:
        fields["category_id"] = gid = f"new:category:{k}"
        changes += [Change("categories", gid, "name", None, {"untrusted": child.name}),
                    Change("categories", gid, "parent_id", None, child.parent_id or MASTER),
                    Change("categories", gid, "type", None, child.type.value)]
    if balance is not None:
        fields["start_balance"] = balance
    if series is None:
        return fields, changes, ()
    fields["funding_recurring_id"] = sid = f"new:series:{k}"
    body = {**series, "category_id": fields.get("category_id", goal.category_id)}
    changes += [Change(TEMPLATE, sid, f, None, _disclose(_SERIES_COLUMNS[f], v)) for f, v in body.items() if v is not None]
    return fields, changes, ((goal, SimpleNamespace(next_due_date=series["next_due_date"], last_spent_date=None,
                                                    occurrence_count=series["occurrence_count"],
                                                    occurrences_elapsed=0)),)


async def _start_steps(ctx: ToolContext, plan: Scenario, goal: PlanGoal) -> tuple[dict[str, Any], list[Change]]:
    """Start's steps for ``goal`` (transient: the goal as it will be), added or
    enabled on a started plan (``plan_lifecycle_service.start_goal``): the goal
    fields they set, and the subcategory, series and Budgets they write."""
    if plan.category_id is None:
        # shortcut: Start always links a category; a started plan that lost it is
        # given a new master by start_goal, which this preview does not disclose.
        raise ToolError("conflict", "this started plan has no category: add the goal in the app")
    with refusals(), plan_goals_router.http_errors():
        await plan_lifecycle_service._check_topology(ctx.db, plan, goal.account_id)
        await plan_lifecycle_service._master_category(ctx.db, plan)  # reads only: the plan has one
    fields, changes, extra = await _goal_start(ctx, plan, goal, 1, datetime.date.today())
    # ``budget_changed`` runs for every goal started late; a Budget it leaves as it is goes unnamed.
    changes += [c for c in await _rebudget(ctx, plan, plan.monthly_budget, extra) if c.before != c.after]
    return fields, changes


def _budget_warning(changes: list[Change]) -> list[str]:
    budgets = {c.id for c in changes if c.entity == "budgets"}
    return [f"{len(budgets)} Budget(s) from the current period on are set to the new amount, "
            "replacing any hand edit or rebalance of them."] if budgets else []


async def _goal_create_preview(ctx: ToolContext, args) -> Preview:
    fields = PlanGoalCreate.model_validate(
        args.model_dump(exclude={"scenario_id", "confirm"}, exclude_unset=True)).model_dump()
    plan = await _goal_plan(ctx, args.scenario_id, fields, confirm=args.confirm)
    steps: list[Change] = []
    if plan.status == ScenarioStatus.ACTIVE and fields["enabled"]:
        set_by_start, steps = await _start_steps(
            ctx, plan, PlanGoal(**fields, org_id=plan.org_id, scenario_id=plan.id))
        fields = {**fields, **set_by_start}
    changes = [Change(GOALS, "new:1", "scenario_id", None, plan.id)]
    changes += [_goal_change("new:1", f, None, v) for f, v in fields.items() if v is not None]
    if plan.currency is None:  # a legacy row given a goal becomes a plan in the org's currency
        currency = await plan_draft_service.plan_currency(ctx.db, org_id=plan.org_id, requested=None)
        changes.append(Change("scenarios", plan.id, "currency", None, currency))
    changes += steps
    return Preview(summary=f"Add a goal to plan {plan.id}", changes=changes, warnings=_budget_warning(steps))


async def _goal_update_preview(ctx: ToolContext, args) -> Preview:
    sent = PlanGoalUpdate.model_validate(
        args.model_dump(include=args.model_fields_set - {"scenario_id", "goal_id", "confirm"})).changes()
    plan = await _goal_plan(ctx, args.scenario_id, sent, confirm=args.confirm,
                            allowed=plan_draft_service.STARTED_GOAL_FIELDS)
    goal = await _goal(ctx, plan, args.goal_id)
    after = dict(sent)
    steps: list[Change] = []
    if plan.status == ScenarioStatus.ACTIVE and sent.get("enabled") and not goal.enabled:
        # A transient copy: setting the locked row would autoflush the preview into the database.
        copy = PlanGoal(**{c.key: getattr(goal, c.key) for c in _GOAL_COLUMNS} | sent)
        set_by_start, steps = await _start_steps(ctx, plan, copy)
        after |= set_by_start
    changes = [_goal_change(goal.id, f, getattr(goal, f), v) for f, v in after.items()]
    changes = [c for c in changes if c.before != c.after]
    if not changes:
        raise ToolError("no_change", "the goal already has these values")
    return Preview(summary=f"Change {', '.join(c.field for c in changes)} of goal {goal.id}",
                   changes=changes + steps, warnings=_budget_warning(steps))


async def _goal_delete_preview(ctx: ToolContext, args) -> Preview:
    plan = await _goal_plan(ctx, args.scenario_id, None)
    goal = await _goal(ctx, plan, args.goal_id)  # nothing references a goal
    return Preview(
        summary=f"Delete goal {goal.id} of plan {plan.id}",
        changes=[Change(GOALS, goal.id, "name", {"untrusted": goal.name}, None)],
        warnings=["A delete cannot be undone."], context={"deleted": [{"entity": GOALS, "id": goal.id}]},
    )


GOAL_PATH = "/api/v1/scenarios/{scenario_id}/goals"
register(route_tool(
    name="plan_goal_create", risk="sensitive", router=plan_goals_router.router, method="POST", path=GOAL_PATH,
    fields=(*GOAL_FIELDS, "confirm"), required=("name", "tier", "kind"), product_area=PLANS, min_role=Role.MEMBER,
    description=(f"Add a goal to a plan. {GOAL_TEXT} On a started plan, only with confirm=true: an "
                 f"enabled goal then gets Start's steps (its subcategory, funding series and Budgets). "
                 f"{CONFIRM}"),
    preview=_goal_create_preview,
))
register(route_tool(
    name="plan_goal_update", risk="sensitive", router=plan_goals_router.router, method="PATCH",
    path=GOAL_PATH + "/{goal_id}", fields=(*GOAL_FIELDS, "confirm"), nullable=GOAL_NULLABLE, product_area=PLANS,
    min_role=Role.MEMBER,
    description=(f"Change a goal of a plan (null clears an optional field). A started plan's goal takes only "
                 f"name, tier, sort_order and enabled=true, with confirm=true; enabling it runs Start's steps "
                 f"for it. A finished plan takes nothing. {CONFIRM}"),
    preview=_goal_update_preview,
))
register(route_tool(
    name="plan_goal_delete", risk="sensitive", router=plan_goals_router.router, method="DELETE",
    path=GOAL_PATH + "/{goal_id}", fields=(), product_area=PLANS, min_role=Role.MEMBER,
    description=f"Delete a goal of a draft plan. {CONFIRM}", preview=_goal_delete_preview,
))
