"""TBD-589: revert an executed agent write (``registry.revert_action`` and
``POST /api/v1/agent/actions/{id}/revert``).

A revert never executes: it stages the inverse of a DONE in-app-reachable
write as a NEW pending confirm-mode in-app action, which the user confirms
through the ordinary confirm path. Every fence names the wrong implementation
it kills in its docstring.
"""
from __future__ import annotations

import dataclasses
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update

from app.agent import actions, registry
from app.agent.registry import ToolError
from app.models.agent_pending_action import ActionRisk, ActionStatus, AgentPendingAction
from app.models.category import Category, CategoryType
from app.models.category_rule import CategoryRule, RuleSource
from app.models.transaction import Transaction
from app.models.user import User

from tests.agent.test_actions import (  # noqa: F401  (fixtures)
    AUTO, MCP, _amount, _audits, _confirm, _invoke, _refused, _row, _rows, _set_amount, _stage,
    engine, factory, scratch, w,
)
from tests.agent.test_agent_routes import _as, _reset_limiter, client  # noqa: F401
from tests.agent.test_set_category import (  # noqa: F401
    TOOL, _add, _make_pair, _stage as _stage_cat, _tx,
)


async def _revert(f, uid, action_id):
    async with f() as db:
        u = await db.get(User, uid)
        return (await registry.revert_action(db, u, action_id))["data"]


async def _done_budget(f, w, amount="150.00", **kw):
    """Budget b1 100 -> ``amount``, executed; returns the action id."""
    out = await _stage(f, w, amount, **kw)
    if kw.get("scope") != "agent:auto":
        await _confirm(f, w["A"]["member"], out["action_id"])
    return out["action_id"]


async def _done_cat(f, a, cat=None):
    out = await _stage_cat(f, a, cat=cat)
    await _confirm(f, a["member"], out["action_id"])
    return out["action_id"]


async def _set_row(f, action_id, **values):
    async with f() as db:
        await db.execute(update(AgentPendingAction).where(AgentPendingAction.id == action_id)
                         .values(**values))
        await db.commit()


# ── F4: the inverse ───────────────────────────────────────────────────────

class _Two(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thing_id: int
    amount: str
    note: str


def _chg(entity, id_, field, before, after):
    return {"entity": entity, "id": id_, "field": field, "before": before, "after": after,
            "currency": None}


def test_f4_inverse_restores_every_primary_field_and_ignores_derived():
    """FENCE F4. Wrong implementation: an inverse from ``changes[0]`` only (the
    second primary field stays at its new value)."""
    spec = SimpleNamespace(args=_Two)
    preview = {"changes": [
        _chg("things", 1, "amount", "1.00", "2.00"),
        _chg("things", 1, "note", "old", "new"),
        _chg("rules", {"untrusted": "tok"}, "amount", "9.00", "2.00"),
    ]}
    out = actions.inverse_args(spec, {"thing_id": 1, "amount": "2.00", "note": "new"}, preview)
    assert out == {"thing_id": 1, "amount": "1.00", "note": "old"}


def test_f4_a_primary_field_the_tool_does_not_take_is_no_inverse():
    spec = SimpleNamespace(args=_Two)
    preview = {"changes": [_chg("things", 1, "colour", "a", "b")]}
    with pytest.raises(ToolError) as exc:
        actions.inverse_args(spec, {"thing_id": 1, "amount": "2.00", "note": "n"}, preview)
    assert (exc.value.code, exc.value.data["reason"]) == ("not_revertible", "no_inverse")


# ── F5: the route stages, never executes ──────────────────────────────────

async def test_f5_post_revert_stages_a_new_in_app_confirm_row(factory, w, client):
    """FENCE F5. Wrong implementations: inheriting the original's auto mode,
    executing directly, staging under the original's token."""
    a = w["A"]
    orig = await _done_budget(factory, w, api_token_id=a["t1"], **AUTO)
    assert (await _amount(factory, a["b1"])) == Decimal("150.00")
    _as(factory, a["member"])
    r = await client.post(f"/api/v1/agent/actions/{orig}/revert")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["requires_confirmation"] is True and body["action_id"] != orig
    assert (await _amount(factory, a["b1"])) == Decimal("150.00")  # nothing executed
    new = await _row(factory, body["action_id"])
    assert (new.status.value, new.mode.value, new.channel.value, new.api_token_id) == (
        "pending", "confirm", "in_app", None)
    assert (await _row(factory, orig)).mode.value == "auto"


# ── F1: end to end through the production path ────────────────────────────

@pytest.mark.parametrize("auto", [False, True], ids=["confirm", "auto"])
@pytest.mark.parametrize("tool", ["budgets_update_amount", "transactions_set_category"])
async def test_f1_revert_then_confirm_restores_the_primary_entity(factory, w, tool, auto):
    """FENCE F1. Apply, ``revert_action``, ``confirm_action``: the primary
    entity equals the original. Wrong implementations: an inverse built from
    ``after``; the test and production builders drifting apart; a direct
    execute."""
    from tests.agent.test_actions import _WRITE_CASES, _NATURAL_KEY, _snapshot

    a = w["A"]
    kw = {"api_token_id": a["t1"], **AUTO} if auto else {}
    args = _WRITE_CASES[tool](a)
    start = await _snapshot(factory)
    out = await _invoke(factory, a["member"], tool, args, **kw)
    if not auto:
        out = await _confirm(factory, a["member"], out["action_id"])
    assert out["status"] == "done"
    [primary] = [c for c in out["changes"] if c["entity"] not in _NATURAL_KEY]
    assert primary["before"] != primary["after"]

    staged = await _revert(factory, a["member"], out["action_id"])
    assert staged["context"]["reverts"] == out["action_id"]
    if tool == "transactions_set_category" and auto:
        # The in-app revert learns the rule the auto original did not: disclosed.
        assert "category_rules" in {c["entity"] for c in staged["changes"]}
        assert staged["warnings"]
    done = await _confirm(factory, a["member"], staged["action_id"])
    assert done["status"] == "done"
    end = await _snapshot(factory)

    def entity(snap):
        row = dict(snap[primary["entity"]][(primary["id"],)])
        row.pop("updated_at", None)
        return row

    assert entity(end) == entity(start)


# ── F3 ────────────────────────────────────────────────────────────────────

async def test_f3_a_preexisting_rule_does_not_hijack_the_inverse(factory, w):
    """FENCE F3. The rule pointed at c3 before the original; the revert
    restores the TRANSACTION to c1, not c3. Wrong implementation: picking the
    change by field name (both are ``category_id``) or ``changes[-1]``."""
    from tests.agent.test_set_category import TOKEN

    a = w["A"]
    [c3] = await _add(factory, Category(org_id=a["org"], name="Third", type=CategoryType.EXPENSE))
    async with factory() as db:
        db.add(CategoryRule(org_id=a["org"], normalized_token=TOKEN, category_id=c3,
                            raw_description_seen="SPOTIFY PREMIUM",
                            source=RuleSource.USER_EDIT))
        await db.commit()
    orig = await _done_cat(factory, a)
    staged = await _revert(factory, a["member"], orig)
    await _confirm(factory, a["member"], staged["action_id"])
    assert (await _tx(factory, a["tx"])).category_id == a["c1"]


# ── F6 / F7: drift ────────────────────────────────────────────────────────

async def test_f6_budget_drift_is_409_and_nothing_executes(factory, w):
    """FENCE F6. 100 -> agent 150 -> member 130. Wrong implementations: a
    blind overwrite; comparing against ``before``; returning 200."""
    a = w["A"]
    orig = await _done_budget(factory, w)
    await _set_amount(factory, a["b1"], "130.00")
    err = await _refused(_revert(factory, a["member"], orig))
    assert err.code == "revert_drift"
    [d] = err.data["drift"]
    assert (d["entity"], d["field"], d["expected"], d["current"]) == (
        "budgets", "amount", "150.00", "130.00")
    assert (await _amount(factory, a["b1"])) == Decimal("130.00")
    pending = [r for r in await _rows(factory) if r.status.value == "pending"]
    assert len(pending) == 1 and pending[0].id == err.data["action_id"]


async def test_f6_category_drift_is_409(factory, w):
    a = w["A"]
    [c3] = await _add(factory, Category(org_id=a["org"], name="Third", type=CategoryType.EXPENSE))
    orig = await _done_cat(factory, a)
    async with factory() as db:
        await db.execute(update(Transaction).where(Transaction.id == a["tx"]).values(category_id=c3))
        await db.commit()
    err = await _refused(_revert(factory, a["member"], orig))
    assert err.code == "revert_drift"
    [d] = err.data["drift"]
    assert (d["entity"], d["expected"], d["current"]) == ("transactions", a["c2"], c3)
    assert (await _tx(factory, a["tx"])).category_id == c3


async def test_f7_only_the_derived_rule_drifting_is_not_drift(factory, w):
    """FENCE F7. Wrong implementation: comparing every change, derived
    included."""
    a = w["A"]
    [c3] = await _add(factory, Category(org_id=a["org"], name="Third", type=CategoryType.EXPENSE))
    orig = await _done_cat(factory, a)
    async with factory() as db:
        await db.execute(update(CategoryRule).where(CategoryRule.org_id == a["org"])
                         .values(category_id=c3))
        await db.commit()
    staged = await _revert(factory, a["member"], orig)
    assert staged["requires_confirmation"] is True


# ── F8 / F9 / F10 / F11 ───────────────────────────────────────────────────

@pytest.mark.parametrize("status", ["pending", "failed", "stale", "cancelled"])
async def test_f8_only_a_done_action_is_revertible(factory, w, status):
    """FENCE F8. Wrong implementation: no status filter."""
    out = await _stage(factory, w)
    if status != "pending":
        await _set_row(factory, out["action_id"], status=ActionStatus(status))
    err = await _refused(_revert(factory, w["A"]["member"], out["action_id"]))
    assert (err.code, err.data["reason"]) == ("not_revertible", "not_done")


async def test_f9_a_stored_sensitive_row_is_not_revertible(factory, w):
    """FENCE F9 (stored). Wrong implementation: checking the current risk only."""
    orig = await _done_budget(factory, w)
    await _set_row(factory, orig, risk=ActionRisk.SENSITIVE)
    err = await _refused(_revert(factory, w["A"]["member"], orig))
    assert (err.code, err.data["reason"]) == ("not_revertible", "not_write")


async def test_f9_a_tool_now_registered_sensitive_is_not_revertible(factory, w, scratch):
    """FENCE F9 (current). Wrong implementation: checking the stored risk only."""
    a = w["A"]
    scratch()
    out = await _invoke(factory, a["member"], "scratch_write", {"budget_id": a["b1"]})
    await _confirm(factory, a["member"], out["action_id"])
    spec = registry._TOOLS["scratch_write"]
    registry._TOOLS["scratch_write"] = dataclasses.replace(spec, risk="sensitive")
    err = await _refused(_revert(factory, a["member"], out["action_id"]))
    assert (err.code, err.data["reason"]) == ("not_revertible", "not_write")


async def test_f10_another_user_or_org_sees_not_found(factory, w):
    """FENCE F10. Wrong implementation: lookup by id, or by org only."""
    orig = await _done_budget(factory, w)
    for uid in (w["A"]["other"], w["B"]["member"]):
        assert (await _refused(_revert(factory, uid, orig))).code == "action_not_found"


async def test_f11_an_mcp_auto_original_is_revertible_in_app_only(factory, w):
    """FENCE F11. Wrong implementations: reusing ``_mine`` (channel and token
    bound) for the lookup; staging the revert under the token."""
    a = w["A"]
    orig = await _done_budget(factory, w, api_token_id=a["t1"], **AUTO)
    staged = await _revert(factory, a["member"], orig)
    err = await _refused(_confirm(factory, a["member"], staged["action_id"],
                                  api_token_id=a["t1"], **MCP))
    assert err.code == "action_not_found"
    assert (await _confirm(factory, a["member"], staged["action_id"]))["status"] == "done"
    assert (await _amount(factory, a["b1"])) == Decimal("100.00")


# ── guards ────────────────────────────────────────────────────────────────

async def test_g1_refusals(factory, w, scratch):
    from app.models.settings import OrgSetting
    from tests.agent.test_set_category import _make_pair

    a = w["A"]
    # retired tool
    scratch("retiring")
    out = await _invoke(factory, a["member"], "retiring", {"budget_id": a["b1"]})
    await _confirm(factory, a["member"], out["action_id"])
    registry._TOOLS.pop("retiring")
    assert (await _refused(_revert(factory, a["member"], out["action_id"]))).code == "tool_retired"
    # budgets switched off
    orig = await _done_budget(factory, w)
    async with factory() as db:
        db.add(OrgSetting(org_id=a["org"], key="orgpref.budgets", value="off"))
        await db.commit()
    assert (await _refused(_revert(factory, a["member"], orig))).code == "feature_disabled"
    async with factory() as db:
        await db.execute(update(OrgSetting).values(value="on"))
        await db.commit()
    # entity deleted
    async with factory() as db:
        from app.models.budget import Budget
        await db.delete(await db.get(Budget, a["b1"]))
        await db.commit()
    assert (await _refused(_revert(factory, a["member"], orig))).code == "not_found"


async def test_g1_transaction_linked_since_is_refused_with_no_open_transaction(factory, w):
    a = w["A"]
    orig = await _done_cat(factory, a)
    await _make_pair(factory, a)
    async with factory() as db:
        u = await db.get(User, a["member"])
        err = await _refused(registry.revert_action(db, u, orig))
        assert not db.in_transaction()
    assert err.code == "unsupported_in_v1"


async def test_g2_repeat_reverts_are_value_based(factory, w):
    a = w["A"]
    orig = await _done_budget(factory, w)
    r1 = await _revert(factory, a["member"], orig)
    r2 = await _revert(factory, a["member"], orig)  # staged while still 150
    await _confirm(factory, a["member"], r1["action_id"])
    assert (await _amount(factory, a["b1"])) == Decimal("100.00")
    # already back at ``before``: nothing to stage
    assert (await _refused(_revert(factory, a["member"], orig))).code == "no_change"
    err = await _refused(_confirm(factory, a["member"], r2["action_id"]))
    assert err.code == "no_change"
    assert (await _row(factory, r2["action_id"])).status.value == "failed"
    # a revert of a revert is an ordinary revertible write
    again = await _revert(factory, a["member"], r1["action_id"])
    await _confirm(factory, a["member"], again["action_id"])
    assert (await _amount(factory, a["b1"])) == Decimal("150.00")


async def test_g3_the_link_survives_a_stale_re_preview_and_reaches_the_audit(factory, w):
    a = w["A"]
    orig = await _done_budget(factory, w)
    staged = await _revert(factory, a["member"], orig)
    assert (await _row(factory, staged["action_id"])).preview_json["context"]["reverts"] == orig
    await _set_amount(factory, a["b1"], "130.00")
    err = await _refused(_confirm(factory, a["member"], staged["action_id"]))
    assert err.code == "preview_stale" and err.data["context"]["reverts"] == orig
    done = await _confirm(factory, a["member"], err.data["action_id"])
    assert done["status"] == "done"
    details = {x.detail["action_id"]: x.detail for x in await _audits(factory)}
    assert details[err.data["action_id"]]["reverts"] == orig
    assert "reverts" not in details[orig]


async def test_g5_a_malformed_stored_preview_is_an_opaque_internal_error(factory, w):
    a = w["A"]
    orig = await _done_budget(factory, w)
    await _set_row(factory, orig, preview_json={"summary": "x", "warnings": [], "context": {}})
    err = await _refused(_revert(factory, a["member"], orig))
    assert err.code == "internal_error"
    assert "changes" not in err.detail and "Error" not in err.detail
