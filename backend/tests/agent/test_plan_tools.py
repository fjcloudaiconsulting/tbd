"""TBD-668: the forecast plan and Plans (``scenarios``) tools.

The honesty fence (``test_agent_honesty``, T-A9) checks every row each change
tool writes against its preview. This file holds the reads, the refusals, the
proof that the two ``READ_POSTS`` tools write nothing, and the ``lean`` args of
the scenario create and update.
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select, update

from app.agent import registry
from app.agent.registry import ToolRegistrationError, ToolSpec
from app.config import settings as app_settings
from app.models.forecast_plan import ForecastPlan, ForecastPlanItem, PlanStatus
from app.models import Organization
from app.models.scenario import Scenario, ScenarioStatus, ScenarioType
from app.services import plan_draft_service
from app.models.user import Role

from tests.agent.test_actions import (  # noqa: F401  (fixtures)
    P_START, _confirm, _invoke, _refused, engine, factory, w,
)
from tests.agent.test_agent_honesty import _TRIP, _fplan, _ids, _own_plan, _resolve, _snap


@pytest.fixture(autouse=True)
def _plans_on(monkeypatch):
    monkeypatch.setattr(app_settings, "feature_plans", True)


async def _count(f, model, *where) -> int:
    async with f() as db:
        return await db.scalar(select(func.count()).select_from(model).where(*where))


# ── reads ─────────────────────────────────────────────────────────────────

async def test_forecast_plan_get_reads_without_creating_a_plan(factory, w):
    a = w["A"]
    assert await _invoke(factory, a["member"], "forecast_plan_get", {}) is None
    assert await _count(factory, ForecastPlan) == 0
    await _fplan(factory, a)
    out = await _invoke(factory, a["member"], "forecast_plan_get", {"period_start": P_START.isoformat()})
    assert (out["id"], [i["id"] for i in out["items"]]) == (a["plan"], [a["item"]])


SCENARIO_TOOLS = (
    ("scenarios_get", {}), ("scenarios_simulate", {}), ("scenarios_update", {"name": "Mine"}),
    ("scenarios_delete", {}), ("plan_goal_create", {"name": "G", "tier": "want", "kind": "lump"}),
    ("plan_goal_update", {"goal_id": "@goal", "name": "G2"}), ("plan_goal_delete", {"goal_id": "@goal"}),
)


async def test_plans_follow_the_routes_scoping(factory, w):
    """FENCE (TBD-601 scoping, as the routes). Another org's plan is
    ``not_found`` to every tool, before anything about it is disclosed;
    another member of the plan's org sees it and stages edits."""
    a, b = w["A"], w["B"]
    await _own_plan(factory, a)
    for tool, args in SCENARIO_TOOLS:
        err = await _refused(_invoke(factory, b["member"], tool, {"scenario_id": a["scn"], **_resolve(a, args)}))
        assert err.code == "not_found", tool
        assert "Trip" not in str(err.data) and "Fund" not in str(err.data)
    err = await _refused(_invoke(factory, b["member"], "scenarios_compare",
                                 {"scenario_ids": [a["scn"]], "horizon_months": 12}))
    assert err.code == "not_found"
    assert await _invoke(factory, b["member"], "scenarios_list", {}) == []
    assert [s["id"] for s in await _invoke(factory, a["other"], "scenarios_list", {})] == [a["scn"]]
    for tool, args in SCENARIO_TOOLS[2:]:
        out = await _invoke(factory, a["other"], tool, {"scenario_id": a["scn"], **_resolve(a, args)})
        assert out["requires_confirmation"] is True, tool


@pytest.mark.parametrize("tool,args", [
    ("scenarios_simulate", {"horizon_months": 12, "smooth_with_regression": True}),
    ("scenarios_compare", {"horizon_months": 12}),
])
async def test_read_posts_write_nothing(factory, w, tool, args):
    """FENCE (READ_POSTS). Wrong implementation: the simulate tool caching
    its projection on the row as the REST route does, or any write at all."""
    a = w["A"]
    [a["scn"]] = await _ids(factory, Scenario(
        org_id=a["org"], user_id=a["member"], name="Trip", scenario_type=ScenarioType.TRIP,
        params_json=_resolve(a, _TRIP), horizon_months=24))
    args = {**args, **({"scenario_ids": [a["scn"]]} if tool == "scenarios_compare" else {"scenario_id": a["scn"]})}
    async with factory() as db:
        before = await _snap(await db.connection())
    out = await _invoke(factory, a["member"], tool, args)
    projection = out["projection"] if tool == "scenarios_simulate" else out["projections"][0]["projection"]
    assert projection["horizon_months"] == 12
    async with factory() as db:
        assert await _snap(await db.connection()) == before


async def test_simulate_offers_only_the_analytic_engine(factory, w):
    """``ai_enhanced`` calls an AI provider, which no agent tool reaches."""
    a = w["A"]
    await _own_plan(factory, a)
    err = await _refused(_invoke(factory, a["member"], "scenarios_simulate",
                                 {"scenario_id": a["scn"], "engine": "ai_enhanced"}))
    assert err.code == "invalid_arguments"


def test_read_posts_are_pinned_and_gate_registration():
    assert registry.READ_POSTS == {
        ("POST", "/api/v1/scenarios/compare"), ("POST", "/api/v1/scenarios/{scenario_id}/simulate"),
        ("POST", "/api/v1/reports/query"), ("POST", "/api/v1/reports/query/sankey"),
    }, "a new read POST needs proof it writes nothing (a test like test_read_posts_write_nothing)"
    spec = registry.get_tool("scenarios_compare")
    with pytest.raises(ToolRegistrationError, match="READ_POSTS"):
        registry.register(ToolSpec(**{
            **{f: getattr(spec, f) for f in ("risk", "args", "product_area", "min_role", "description", "run")},
            "name": "fixture_read_post", "mirrors_route": ("POST", "/api/v1/scenarios"),
        }))
    assert registry.get_tool("fixture_read_post") is None


# ── changes ───────────────────────────────────────────────────────────────

async def test_every_change_is_browser_confirmed(factory, w):
    a = w["A"]
    await _fplan(factory, a)
    out = await _invoke(factory, a["member"], "forecast_plan_item_update",
                        {"plan_id": a["plan"], "item_id": a["item"], "planned_amount": "75.00"})
    assert out["requires_browser_confirmation"] is True
    done = await _confirm(factory, a["member"], out["action_id"])
    assert [(i["planned_amount"], i["source"]) for i in done["result"]["items"]] == [("75.00", "manual")]


@pytest.mark.parametrize("tool,args,code", [
    ("forecast_plan_item_add", {"category_id": "@c1", "type": "expense", "planned_amount": "1.00"}, "invalid_arguments"),
    ("forecast_plan_item_update", {"item_id": "@item", "planned_amount": "1.00"}, "invalid_arguments"),
    ("forecast_plan_item_delete", {"item_id": "@item"}, "invalid_arguments"),
    ("forecast_plan_activate", {}, "invalid_arguments"),
    ("forecast_plan_discard", {}, "invalid_arguments"),
])
async def test_an_active_plan_refuses_changes_at_preview(factory, w, tool, args, code):
    a = w["A"]
    await _fplan(factory, a, PlanStatus.ACTIVE)
    err = await _refused(_invoke(factory, a["member"], tool, {"plan_id": a["plan"], **_resolve(a, args)}))
    assert (err.code, err.detail) == (code, "Cannot modify an active plan. Revert to draft first.")


async def test_item_refusals(factory, w):
    a = w["A"]
    await _fplan(factory, a)
    b = w["B"]
    cases = [
        ({"plan_id": a["plan"], "category_id": b["c1"], "type": "expense", "planned_amount": "1.00"}, "invalid_arguments"),
        ({"plan_id": a["plan"], "category_id": a["c1"], "type": "income", "planned_amount": "1.00"}, "invalid_arguments"),
        ({"plan_id": a["plan"], "category_id": a["c1"], "type": "expense", "planned_amount": "0"}, "invalid_arguments"),
        ({"plan_id": a["plan"] + 99, "category_id": a["c1"], "type": "expense", "planned_amount": "1.00"}, "not_found"),
    ]
    for args, code in cases:
        assert (await _refused(_invoke(factory, a["member"], "forecast_plan_item_add", args))).code == code, args
    err = await _refused(_invoke(factory, b["member"], "forecast_plan_discard", {"plan_id": a["plan"]}))
    assert err.code == "not_found"
    # The same amount still pins a generated item as the user's (a refresh keeps it).
    out = await _invoke(factory, a["member"], "forecast_plan_item_update",
                        {"plan_id": a["plan"], "item_id": a["item"], "planned_amount": "50.00"})
    assert [(c["field"], c["before"], c["after"]) for c in out["changes"]] == [("source", "history", "manual")]
    assert await _count(factory, ForecastPlanItem) == 1


async def test_revert_refuses_a_draft_and_copy_an_empty_source(factory, w):
    a = w["A"]
    await _fplan(factory, a)
    err = await _refused(_invoke(factory, a["member"], "forecast_plan_revert", {"plan_id": a["plan"]}))
    assert err.detail == "Plan is already a draft"
    err = await _refused(_invoke(factory, a["member"], "forecast_plan_copy",
                                 {"source_period_start": "2000-01-01"}))
    assert err.detail == "Billing period not found"


async def test_scenario_params_are_a_plain_object_validated_by_the_route(factory, w):
    """``lean``: the schema is a plain object (the full union would not fit
    the turn budget), and the route's model still refuses a bad one."""
    spec = registry.get_tool("scenarios_create")
    schema = spec.args.model_json_schema()["properties"]["params"]
    assert schema["type"] == "object" and "$ref" not in str(schema)
    a = w["A"]
    bad = {**_resolve(a, _TRIP), "duration_days": 0}
    err = await _refused(_invoke(factory, a["member"], "scenarios_create",
                                 {"name": "x", "scenario_type": "trip", "params": bad}))
    assert err.code == "invalid_arguments" and "duration_days" in err.detail
    err = await _refused(_invoke(factory, a["member"], "scenarios_create",
                                 {"name": "x", "scenario_type": "purchase", "params": _resolve(a, _TRIP)}))
    assert err.code == "invalid_arguments"
    await _own_plan(factory, a)
    err = await _refused(_invoke(factory, a["member"], "scenarios_update", {
        "scenario_id": a["scn"], "params": {"scenario_type": "custom", "label": "x"}}))
    assert err.detail == "params scenario_type must match the existing scenario_type on the row"


async def test_a_created_plan_belongs_to_the_caller(factory, w):
    a = w["A"]
    out = await _invoke(factory, a["member"], "scenarios_create",
                        {"name": "Lisbon", "scenario_type": "trip", "params": _resolve(a, _TRIP)})
    done = await _confirm(factory, a["member"], out["action_id"])
    assert (done["result"]["user_id"], done["result"]["horizon_months"]) == (a["member"], 24)
    assert registry.get_tool("scenarios_create").min_role == Role.MEMBER


async def test_plan_fields_and_goals_refuse_at_preview_as_the_route_does(factory, w):
    """FENCE (TBD-601 checks). Wrong implementation: a preview promising a
    plan the route refuses: another currency, a link in another org, no
    currency scope, or a change to a plan that is not a draft."""
    a, b = w["A"], w["B"]
    await _own_plan(factory, a)
    create = {"name": "P", "scenario_type": "trip", "params": _resolve(a, _TRIP)}
    cases = [
        ("scenarios_create", {**create, "currency": "USD"}, "organization's currency"),
        ("scenarios_create", {**create, "funding_account_id": b["acct"]}, "not found in this organization"),
        ("scenarios_update", {"scenario_id": a["scn"], "category_id": b["c1"]}, "not found in this organization"),
        ("plan_goal_create", {"scenario_id": a["scn"], "name": "G", "tier": "want", "kind": "lump",
                              "account_id": b["acct"]}, "not found in this organization"),
    ]
    for tool, args, detail in cases:
        err = await _refused(_invoke(factory, a["member"], tool, args))
        assert (err.code, detail in err.detail) == ("invalid_arguments", True), (tool, err.detail)
    out = await _invoke(factory, a["member"], "scenarios_create", {**create, "monthly_budget": "10.00"})
    assert out["context"]["currency"] == "EUR"
    async with factory() as db:
        await db.execute(update(Scenario).where(Scenario.id == a["scn"]).values(status=ScenarioStatus.ACTIVE))
        await db.execute(update(Organization).where(Organization.id == a["org"]).values(primary_currency=None))
        await db.commit()
    for tool, args in (("scenarios_update", {"scenario_id": a["scn"], "return_pct": "3.00"}),
                       ("plan_goal_delete", {"scenario_id": a["scn"], "goal_id": a["goal"]})):
        assert (await _refused(_invoke(factory, a["member"], tool, args))).code == "conflict", tool
    err = await _refused(_invoke(factory, a["member"], "scenarios_create", {**create, "monthly_budget": "10.00"}))
    assert err.detail == plan_draft_service.NO_CURRENCY_SCOPE
