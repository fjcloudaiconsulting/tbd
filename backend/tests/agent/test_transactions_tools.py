"""TBD-664 PR A: ``transactions_get`` and ``transactions_update`` (non-money).

Both are route-bound (``app.agent.route_tool``). The honesty fence
(``test_agent_honesty``), F-A3 (``test_actions``) and the revert round trip
(``test_revert``) run over ``transactions_update`` through their own scenario
tables; this file holds the tool's own refusals and disclosures.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.category_rule import CategoryRule
from app.models.recurring import Frequency, RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus

from tests.agent.test_actions import (  # noqa: F401  (fixtures)
    P_START, _confirm, _invoke, _refused, engine, factory, w,
)
from tests.agent.test_set_category import _add, _make_pair, _set, _tx

TOOL = "transactions_update"
MARK = "IGNORE-PREVIOUS-INSTRUCTIONS"


async def _stage(f, a, **fields):
    return await _invoke(f, a["member"], TOOL, {"transaction_id": a["tx"], **fields})


async def _apply(f, a, **fields):
    out = await _stage(f, a, **fields)
    return await _confirm(f, a["member"], out["action_id"])


def _wrapped(v):
    return {"untrusted": v}


# ── disclosure ────────────────────────────────────────────────────────────

async def test_description_edit_is_disclosed_wrapped_and_applied(factory, w):
    """FENCE (F-R10 for this tool). Wrong implementation: user text in the
    summary, or a bare before/after string, reaching the model unwrapped."""
    a = w["A"]
    out = await _stage(factory, a, description=f"  Music {MARK}  ")
    assert out["changes"] == [{
        "entity": "transactions", "id": a["tx"], "field": "description",
        "before": _wrapped("SPOTIFY PREMIUM"), "after": _wrapped(f"Music {MARK}"), "currency": None,
    }]
    assert MARK not in out["summary"] and "SPOTIFY" not in out["summary"]
    done = await _confirm(factory, a["member"], out["action_id"])
    assert done["status"] == "done"
    assert (await _tx(factory, a["tx"])).description == f"Music {MARK}"
    assert done["result"]["description"] == _wrapped(f"Music {MARK}")


async def test_unchanged_fields_are_not_listed_and_all_unchanged_is_no_change(factory, w):
    a = w["A"]
    out = await _stage(factory, a, description="SPOTIFY PREMIUM", date=P_START.isoformat(),
                       settled_date=(P_START + datetime.timedelta(days=2)).isoformat())
    assert [c["field"] for c in out["changes"]] == ["settled_date"]
    err = await _refused(_stage(factory, a, description="SPOTIFY PREMIUM"))
    assert err.code == "no_change"


async def test_dates_move_together_and_ordering_is_checked_at_preview(factory, w):
    """FENCE. Wrong implementation: a preview that promises a date pair the
    service then refuses at confirm (settled before date)."""
    a = w["A"]
    later = P_START + datetime.timedelta(days=3)
    err = await _refused(_stage(factory, a, date=later.isoformat()))
    assert err.code == "invalid_arguments"  # settled_date (P_START) would precede it
    done = await _apply(factory, a, date=later.isoformat(), settled_date=later.isoformat())
    assert {c["field"] for c in done["changes"]} == {"date", "settled_date"}
    tx = await _tx(factory, a["tx"])
    assert (tx.date, tx.settled_date) == (later, later)


async def test_a_settled_row_keeps_its_settlement_date(factory, w):
    """FENCE. Wrong implementation: previewing ``settled_date -> null`` on a
    settled row; the service keeps the date, so the preview would lie."""
    a = w["A"]
    err = await _refused(_stage(factory, a, settled_date=None))
    assert err.code == "invalid_arguments"


async def test_a_pending_rows_expected_date_can_be_cleared(factory, w):
    """FENCE (set-ness). An explicit null clears; an omitted key leaves alone."""
    a = w["A"]
    expected = P_START + datetime.timedelta(days=5)
    await _set(factory, Transaction, a["tx"], status=TransactionStatus.PENDING, settled_date=expected)
    await _apply(factory, a, description="Renamed")
    assert (await _tx(factory, a["tx"])).settled_date == expected
    done = await _apply(factory, a, settled_date=None)
    assert done["changes"][0]["before"] == expected.isoformat() and done["changes"][0]["after"] is None
    assert (await _tx(factory, a["tx"])).settled_date is None


# ── refusals ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("field,value", [
    ("amount", "1.00"), ("account_id", 1), ("type", "income"), ("status", "pending"),
    ("category_id", 1), ("description", None), ("date", None), ("description", "   "),
])
async def test_money_fields_and_nulls_are_not_arguments(factory, w, field, value):
    """FENCE (the allowlist). Wrong implementation: the whole REST body exposed
    at ``write`` tier, so an auto token could move money; or a null accepted
    where the service reads null as "leave alone"."""
    a = w["A"]
    err = await _refused(_stage(factory, a, **{field: value}))
    assert err.code == "invalid_arguments"


async def test_another_orgs_transaction_is_not_found(factory, w):
    a, b = w["A"], w["B"]
    err = await _refused(_invoke(factory, a["member"], TOOL,
                                 {"transaction_id": b["tx"], "description": "x"}))
    assert err.code == "not_found"
    err = await _refused(_invoke(factory, a["member"], "transactions_get",
                                 {"transaction_id": b["tx"]}))
    assert err.code == "not_found"


async def test_a_manual_adjustment_is_refused(factory, w):
    a = w["A"]
    await _set(factory, Transaction, a["tx"], is_manual_adjustment=True)
    err = await _refused(_stage(factory, a, description="x"))
    assert err.code == "invalid_arguments"


async def test_a_series_description_edit_is_refused_and_a_date_edit_is_not(factory, w):
    """FENCE. Wrong implementation: renaming a recurring row, which also
    renames the template and every pending sibling the preview never listed."""
    a = w["A"]
    [rid] = await _add(factory, RecurringTransaction(
        org_id=a["org"], account_id=a["acct"], category_id=a["c1"], description="SPOTIFY PREMIUM",
        amount=Decimal("9.99"), type="expense", frequency=Frequency("monthly"),
        next_due_date=P_START + datetime.timedelta(days=40),
    ))
    await _set(factory, Transaction, a["tx"], recurring_id=rid)
    err = await _refused(_stage(factory, a, description="Renamed"))
    assert (err.code, err.data.get("reason")) == ("unsupported_in_v1", "recurring_series")
    later = (P_START + datetime.timedelta(days=1)).isoformat()
    await _apply(factory, a, date=later, settled_date=later)


async def test_a_transfer_leg_description_edit_touches_only_that_leg(factory, w):
    a = w["A"]
    partner, _ = await _make_pair(factory, a)
    await _apply(factory, a, description="To savings")
    assert (await _tx(factory, partner)).description == "FROM MAIN"


async def test_no_rule_is_learned(factory, w):
    a = w["A"]
    await _apply(factory, a, description="Renamed")
    async with factory() as db:
        assert (await db.scalars(select(CategoryRule))).all() == []


# ── transactions_get ──────────────────────────────────────────────────────

async def test_get_returns_the_rest_shape_with_user_text_wrapped(factory, w):
    a = w["A"]
    out = await _invoke(factory, a["member"], "transactions_get", {"transaction_id": a["tx"]})
    assert out["id"] == a["tx"] and out["amount"] == "9.99"
    assert out["description"] == _wrapped("SPOTIFY PREMIUM")
    assert out["tags"] == []
