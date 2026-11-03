"""TBD-666: the budget and category tools.

The honesty fence (``test_agent_honesty``, T-A9) checks every row each tool
writes against its preview, F-A3 and F1 cover the two ``write`` tools
(``budgets_rebalance``, ``categories_update``) and their revert. This file holds
the refusals, the results, and the ``route_tool`` pieces they needed: two
product-area gates on one route, a handler that audits a client address, and a
204 with no body.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, func, select

from app.agent import registry
from app.agent.registry import ToolRegistrationError, ToolSpec
from app.models.audit_event import AuditEvent
from app.models.billing import BillingPeriod
from app.models.budget import Budget
from app.models.category import Category, CategoryType
from app.models.settings import OrgSetting
from app.models.user import Role, User
from app.services.feature_gate import Feature

from tests.agent.test_actions import (  # noqa: F401  (fixtures)
    P_START, _confirm, _invoke, _refused, _rows, engine, factory, w,
)
from tests.agent.test_agent_honesty import NEXT, _floor, _forecast, _ids, _rent, _subs


async def _apply(f, a, tool, args, uid=None):
    out = await _invoke(f, uid or a["member"], tool, args)
    return out, await _confirm(f, uid or a["member"], out["action_id"])


async def _count(f, model, *where) -> int:
    async with f() as db:
        return await db.scalar(select(func.count()).select_from(model).where(*where))


# ── budgets ───────────────────────────────────────────────────────────────

async def test_create_a_budget_for_me(factory, w):
    """The owner's first test case: refused before TBD-666."""
    a = w["A"]
    await _rent(factory, a)
    out, done = await _apply(factory, a, "budgets_create", {"category_id": a["rent"], "amount": "900.00"})
    assert out["requires_browser_confirmation"] is True
    assert out["context"]["category_name"] == {"untrusted": "Rent"}
    assert (done["result"]["category_id"], done["result"]["amount"]) == (a["rent"], "900.00")
    assert await _count(factory, Budget, Budget.category_id == a["rent"], Budget.period_start == P_START) == 1


async def test_create_refuses_what_the_service_refuses(factory, w):
    a = w["A"]
    [sub] = await _ids(factory, Category(org_id=a["org"], name="Sub", type=CategoryType.EXPENSE, parent_id=a["c1"]))
    cases = [
        ({"category_id": sub, "amount": "1.00"}, "invalid_arguments"),
        ({"category_id": a["c1"], "amount": "1.00"}, "conflict"),  # Food is budgeted already
        ({"category_id": w["B"]["c1"], "amount": "1.00"}, "invalid_arguments"),  # another org's
        ({"category_id": a["c1"], "amount": "1.00", "period_start": "2001-01-01"}, "invalid_arguments"),
    ]
    for args, code in cases:
        assert (await _refused(_invoke(factory, a["member"], "budgets_create", args))).code == code, args
    assert await _rows(factory) == []


async def test_create_with_no_open_period_refuses_instead_of_creating_one(factory, w):
    """Wrong implementation: letting the handler's ``get_current_period``
    auto-create a period the preview never named."""
    a = w["A"]
    async with factory() as db:
        await db.execute(delete(BillingPeriod).where(BillingPeriod.org_id == a["org"]))
        await db.commit()
    for tool, args in (("budgets_create", {"category_id": a["c1"], "amount": "1.00"}),
                       ("budgets_draft_next", {"period_start": P_START.isoformat()})):
        assert (await _refused(_invoke(factory, a["member"], tool, args))).code == "no_open_period"
    assert await _count(factory, BillingPeriod, BillingPeriod.org_id == a["org"]) == 0


async def test_from_forecast_needs_both_product_areas(factory, w):
    """Wrong implementation: gating the seed on Budgets only, while REST also
    closes it when the org switched Forecast off."""
    a = w["A"]
    await _forecast(factory, a)
    async with factory() as db:
        db.add(OrgSetting(org_id=a["org"], key="orgpref.forecast", value="off"))
        await db.commit()
    err = await _refused(_invoke(factory, a["member"], "budgets_from_forecast", {}))
    assert (err.code, err.detail) == ("feature_disabled", "forecast")
    # budgets_list carries only the Budgets gate and still works
    assert (await _invoke(factory, a["member"], "budgets_list", {}))["budgets"]


async def test_from_forecast_without_a_plan_or_with_nothing_left_is_refused(factory, w):
    a = w["A"]
    err = await _refused(_invoke(factory, a["member"], "budgets_from_forecast", {}))
    assert err.code == "invalid_arguments" and "forecast plan" in err.detail
    await _forecast(factory, a)
    await _apply(factory, a, "budgets_from_forecast", {})
    err = await _refused(_invoke(factory, a["member"], "budgets_from_forecast", {}))
    assert err.code == "no_change"


async def test_copy_refuses_an_empty_source(factory, w):
    a = w["A"]
    later = (P_START.replace(year=P_START.year + 1)).isoformat()
    async with factory() as db:
        db.add(BillingPeriod(org_id=a["org"], start_date=P_START.replace(year=P_START.year + 1)))
        await db.commit()
    err = await _refused(_invoke(factory, a["member"], "budgets_copy_from_period",
                                 {"source_period_start": later}))
    assert (err.code, err.detail) == ("invalid_arguments", "Source period has no budgets to copy")


async def test_draft_returns_the_suggestions_and_creates_no_budget(factory, w):
    a = w["A"]
    out, done = await _apply(factory, a, "budgets_draft_next", {"period_start": NEXT.isoformat()})
    assert done["result"]["period_start"] == NEXT.isoformat()
    assert {c["entity"] for c in out["changes"]} == {"billing_periods"}
    assert await _count(factory, Budget, Budget.period_start == NEXT) == 0


async def test_rebalance_refuses_a_stale_amount_or_a_nonzero_net(factory, w):
    a = w["A"]
    item = {"budget_id": a["b1"], "expected_amount": "100.00", "amount": "90.00"}
    stale = [{**item, "expected_amount": "99.00"}, {"budget_id": a["b2"], "expected_amount": "50.00", "amount": "59.00"}]
    assert (await _refused(_invoke(factory, a["member"], "budgets_rebalance", {"items": stale}))).code == "conflict"
    uneven = [item, {"budget_id": a["b2"], "expected_amount": "50.00", "amount": "50.00"}]
    err = await _refused(_invoke(factory, a["member"], "budgets_rebalance", {"items": uneven}))
    assert (err.code, err.detail) == ("invalid_arguments", "Changes must net to zero.")
    other = [item, {"budget_id": w["B"]["b2"], "expected_amount": "50.00", "amount": "60.00"}]
    assert (await _refused(_invoke(factory, a["member"], "budgets_rebalance", {"items": other}))).code == "not_found"
    assert await _rows(factory) == []


async def test_rebalance_is_auto_applied_and_delete_never_is(factory, w):
    """The tiers: a rebalance is a ``write`` (an auto token applies it), a
    delete is ``sensitive`` (it stays pending for the person)."""
    a = w["A"]
    kw = {"channel": "mcp", "scope": "agent:auto", "api_token_id": a["t1"]}
    items = [{"budget_id": a["b1"], "expected_amount": "100.00", "amount": "60.00"},
             {"budget_id": a["b2"], "expected_amount": "50.00", "amount": "90.00"}]
    assert (await _invoke(factory, a["member"], "budgets_rebalance", {"items": items}, **kw))["status"] == "done"
    out = await _invoke(factory, a["member"], "budgets_delete", {"budget_id": a["b1"]}, **kw)
    assert out["requires_browser_confirmation"] is True
    assert (await _refused(_confirm(factory, a["member"], out["action_id"], **kw))).code == "requires_browser_confirmation"
    async with factory() as db:
        assert (await db.get(Budget, a["b1"])).amount == Decimal("60.00")


# ── categories ────────────────────────────────────────────────────────────

async def test_update_runs_the_handler_with_no_client_address(factory, w):
    """``client_ip`` is supplied as none in-process: the route's own audit row
    is written, with no address. Wrong implementation: a stub ``Request``."""
    a = w["A"]
    _, done = await _apply(factory, a, "categories_update", {"category_id": a["c1"], "name": "Groceries"})
    assert done["result"]["name"] == {"untrusted": "Groceries"}
    async with factory() as db:
        audit = await db.scalar(select(AuditEvent).where(AuditEvent.event_type == "category.renamed"))
    assert audit.ip_address is None and audit.detail["new_name"] == "Groceries"


async def test_update_refuses_a_type_change_the_service_refuses(factory, w):
    a = w["A"]
    await _subs(factory, a)
    err = await _refused(_invoke(factory, a["member"], "categories_update",
                                 {"category_id": a["c1"], "type": "income"}))  # Food has an expense row
    assert err.code == "invalid_arguments"
    err = await _refused(_invoke(factory, a["member"], "categories_update",
                                 {"category_id": a["cinema"], "type": "both"}))
    assert (err.code, err.detail) == (
        "invalid_arguments", "Subcategory type must match its parent. Update the master category instead.")


async def test_move_refuses_a_name_collision_without_naming_it(factory, w):
    """A refusal detail never carries category names (user text)."""
    a = w["A"]
    await _subs(factory, a)
    twice = [{"subcategory_id": a["cinema"], "target_parent_id": a["c1"]}] * 2
    err = await _refused(_invoke(factory, a["member"], "categories_batch_move", {"moves": twice}))
    assert err.code == "invalid_arguments"
    await _ids(factory, Category(org_id=a["org"], name="cinema", type=CategoryType.EXPENSE, parent_id=a["c1"]))
    err = await _refused(_invoke(factory, a["member"], "categories_move",
                                 {"category_id": a["cinema"], "target_parent_id": a["c1"]}))
    assert err.code == "conflict" and "inema" not in err.detail
    assert await _rows(factory) == []


async def test_delete_refusals_and_the_204_path(factory, w):
    a = w["A"]
    await _subs(factory, a)
    err = await _refused(_invoke(factory, a["member"], "categories_delete", {"category_id": a["c2"]}))
    assert err.code == "conflict" and "Cinema" not in err.detail  # has subcategories
    await _floor(factory, a)
    err = await _refused(_invoke(factory, a["member"], "categories_delete", {"category_id": 10**6}))
    assert err.code == "not_found"
    _, done = await _apply(factory, a, "categories_delete", {"category_id": a["games"]})
    assert done["result"] is None  # REST answers 204 with no body
    assert await _count(factory, Category, Category.id == a["games"]) == 0


async def test_delete_in_use_needs_a_target(factory, w):
    a = w["A"]
    await _floor(factory, a)
    err = await _refused(_invoke(factory, a["member"], "categories_delete", {"category_id": a["c1"]}))
    assert err.code == "invalid_arguments" and "target_category_id" in err.detail


async def test_restore_is_owner_only_and_a_full_restore_is_too_large_to_preview(factory, w):
    """A preview names every inserted column, so restoring the whole seed
    (an org with none of it) exceeds the stored-preview limit: refused, the
    person restores from Settings. Nothing is written either way."""
    a = w["A"]
    err = await _refused(_invoke(factory, a["admin"], "categories_restore_recommended", {}))
    assert err.code == "insufficient_role"
    async with factory() as db:
        (await db.get(User, a["member"])).role = Role.OWNER
        await db.commit()
    err = await _refused(_invoke(factory, a["member"], "categories_restore_recommended", {}))
    assert err.code == "payload_too_large"
    assert await _count(factory, Category, Category.is_system.is_(True)) == 0


# ── route_tool ────────────────────────────────────────────────────────────

class _NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _noop(*a):
    return None


def test_extra_areas_sit_beside_a_product_area():
    spec = ToolSpec(
        name="extra_areas_alone", risk="read", args=_NoArgs, product_area=None,
        min_role=Role.MEMBER, mirrors_route=("GET", "/api/v1/budgets"), description="t",
        run=_noop, extra_areas=(Feature.FORECAST,),
    )
    with pytest.raises(ToolRegistrationError, match="extra_areas"):
        registry.register(spec)
    assert registry.get_tool("extra_areas_alone") is None


def test_a_route_tool_must_declare_every_product_gate_of_its_route():
    from app.agent.route_tool import route_tool
    from app.routers import budgets

    with pytest.raises(ToolRegistrationError, match="forecast"):
        route_tool(
            name="seed_without_forecast", risk="sensitive", router=budgets.router, method="POST",
            path="/api/v1/budgets/from-forecast", fields=("period_start",),
            product_area=Feature.BUDGETS, min_role=Role.MEMBER, description="t", preview=_noop,
        )
