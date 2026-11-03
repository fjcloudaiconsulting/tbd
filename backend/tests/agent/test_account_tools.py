"""TBD-665: the account, account type and cycle payment tools.

The honesty fence (``test_agent_honesty``, T-A9) checks every row each one
writes against its preview, and the revert fence (``test_revert``, F1) runs
the two ``write`` tools. This file holds their refusals (each the route's own,
answered at preview), their results, and the browser-only confirm on a new
sensitive tool.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import Account, AccountType, Transaction
from app.models.cc_cycle_payment import CcCyclePayment

from tests.agent.test_actions import (  # noqa: F401  (fixtures)
    _confirm, _invoke, _refused, engine, factory, w,
)
from tests.agent.test_agent_honesty import CYCLE_YEAR, _adjustable, _cc, _cycle_paid, _ids
from tests.agent.test_set_category import _set

CYCLE = {"year": CYCLE_YEAR, "month": 6}


async def _get(f, model, row_id):
    async with f() as db:
        return await db.get(model, row_id)


async def _zero_account(f, a, name="Spare") -> int:
    [aid] = await _ids(f, Account(org_id=a["org"], name=name, account_type_id=a["type"],
                                  balance=Decimal("0.00"), currency="EUR", is_default=False))
    return aid


# ── the browser-only confirm ──────────────────────────────────────────────

async def test_an_agent_stages_an_account_delete_but_only_the_person_confirms_it(factory, w):
    """FENCE (TBD-664 tier). Wrong implementation: ``accounts_delete`` declared
    ``write``, so an agent token confirms (or auto-applies) its own delete."""
    a = w["A"]
    aid = await _zero_account(factory, a)
    kw = {"channel": "mcp", "scope": "agent:auto", "api_token_id": a["t1"]}
    out = await _invoke(factory, a["member"], "accounts_delete", {"account_id": aid}, **kw)
    assert out["requires_browser_confirmation"] is True
    assert (await _refused(_confirm(factory, a["member"], out["action_id"], **kw))).code == (
        "requires_browser_confirmation")
    assert await _get(factory, Account, aid) is not None
    await _confirm(factory, a["member"], out["action_id"])
    assert await _get(factory, Account, aid) is None


# ── accounts ──────────────────────────────────────────────────────────────

async def test_accounts_refuse_at_preview_what_the_routes_refuse(factory, w):
    a = w["A"]
    for tool, args, code in (
        ("accounts_update", {"account_id": a["acct"], "is_active": False}, "conflict"),  # balance 1000
        ("accounts_delete", {"account_id": a["acct"]}, "conflict"),
        ("accounts_get", {"account_id": w["B"]["acct"]}, "not_found"),
    ):
        assert (await _refused(_invoke(factory, a["member"], tool, args))).code == code, tool
    await _set(factory, Account, a["acct"], balance=Decimal("0.00"))
    err = await _refused(_invoke(factory, a["member"], "accounts_delete", {"account_id": a["acct"]}))
    assert (err.code, err.detail) == ("conflict", "Cannot delete: 1 transaction(s) use this account")
    # a payment source must be a checking, savings or cash account
    other = await _zero_account(factory, a)
    err = await _refused(_invoke(factory, a["member"], "accounts_update",
                                 {"account_id": a["acct"], "payment_source_account_id": other}))
    assert err.code == "invalid_arguments"


async def test_auto_refuses_a_default_switch_that_clears_another_account(factory, w):
    """FENCE (``ToolContext.auto``). Wrong implementation: an unconfirmed write
    clearing the current default account, a row the person never saw."""
    a = w["A"]
    other = await _zero_account(factory, a)
    args = {"account_id": other, "is_default": True}
    auto = {"channel": "mcp", "scope": "agent:auto", "api_token_id": a["t1"]}
    err = await _refused(_invoke(factory, a["member"], "accounts_update", args, **auto))
    assert (err.code, err.data["reason"]) == ("unsupported_in_v1", "derived_rows")
    assert (await _get(factory, Account, a["acct"])).is_default is True
    out = await _invoke(factory, a["member"], "accounts_update", args)  # confirmed in-app: fine
    assert [c["id"] for c in out["changes"]] == [other, a["acct"]]


async def test_account_create_returns_the_route_response(factory, w):
    a = w["A"]
    out = await _invoke(factory, a["member"], "accounts_create", {
        "name": "Brokerage", "account_type_id": a["type"], "opening_balance": "40.00",
        "opening_balance_date": "2026-01-02",
    })
    assert out["warnings"] == ["The account's balance starts at its opening balance."]
    done = await _confirm(factory, a["member"], out["action_id"])
    assert (done["result"]["name"], done["result"]["balance"]) == ({"untrusted": "Brokerage"}, "40.00")


# ── accounts_adjust_balance ───────────────────────────────────────────────

async def test_adjust_balance_has_the_routes_gates(factory, w):
    a = w["A"]
    args = {"account_id": a["acct"], "target_balance": "1200.00"}
    assert (await _refused(_invoke(factory, a["member"], "accounts_adjust_balance", args))).code == (
        "insufficient_role")
    err = await _refused(_invoke(factory, a["admin"], "accounts_adjust_balance", args))
    assert (err.code, err.detail) == ("forbidden", "Manual balance adjustment is disabled for this organization")
    await _adjustable(factory, a)
    err = await _refused(_invoke(factory, a["admin"], "accounts_adjust_balance",
                                 {**args, "target_balance": "1000.00"}))
    assert err.code == "no_change"
    err = await _refused(_invoke(factory, a["admin"], "accounts_adjust_balance",
                                 {**args, "account_id": w["B"]["acct"]}))
    assert err.code == "not_found"


async def test_adjust_balance_books_one_adjustment_and_returns_the_route_response(factory, w):
    a = w["A"]
    await _adjustable(factory, a)
    out = await _invoke(factory, a["admin"], "accounts_adjust_balance",
                        {"account_id": a["acct"], "target_balance": "1200.00", "reason": "statement"})
    assert out["changes"][-1] == {"entity": "accounts", "id": a["acct"], "field": "balance_delta",
                                  "before": None, "after": "200.00", "currency": "EUR"}
    done = await _confirm(factory, a["admin"], out["action_id"])
    r = done["result"]
    assert (r["old_balance"], r["new_balance"], r["delta"]) == ("1000.00", "1200.00", "200.00")
    assert (await _get(factory, Account, a["acct"])).balance == Decimal("1200.00")
    tx = await _get(factory, Transaction, r["transaction_id"])
    assert tx.is_manual_adjustment and tx.description == "Balance adjustment: 1000.00 -> 1200.00 (statement)"


# ── account types ─────────────────────────────────────────────────────────

async def test_account_types_refuse_a_system_type_and_a_used_one(factory, w):
    a = w["A"]
    err = await _refused(_invoke(factory, a["member"], "account_types_delete", {"type_id": a["type"]}))
    assert (err.code, err.detail) == ("conflict", "Cannot delete: 1 account(s) use this type")
    await _set(factory, AccountType, a["type"], is_system=True)
    for tool, args in (("account_types_update", {"type_id": a["type"], "name": "X"}),
                       ("account_types_delete", {"type_id": a["type"]})):
        assert (await _refused(_invoke(factory, a["member"], tool, args))).code == "conflict", tool


# ── cycle payments ────────────────────────────────────────────────────────

async def test_cycle_payments_are_owner_or_admin_and_take_the_routes_cycle_rules(factory, w):
    a = w["A"]
    await _cc(factory, a)
    args = {"account_id": a["cc"], **CYCLE, "amount": "80.00"}
    assert (await _refused(_invoke(factory, a["member"], "cycle_payments_create", args))).code == (
        "insufficient_role")
    for over, code in (
        ({"account_id": a["acct"]}, "invalid_arguments"),  # not a credit card
        ({"amount": "0.00"}, "invalid_arguments"),
        ({"year": 2020}, "conflict"),  # a past cycle
        ({"account_id": w["B"]["acct"]}, "not_found"),
    ):
        err = await _refused(_invoke(factory, a["admin"], "cycle_payments_create", {**args, **over}))
        assert err.code == code, over
    err = await _refused(_invoke(factory, a["admin"], "cycle_payments_delete", {"account_id": a["cc"], **CYCLE}))
    assert err.code == "not_found"
    await _cycle_paid(factory, a)
    err = await _refused(_invoke(factory, a["admin"], "cycle_payments_create", args))
    assert (err.code, err.detail) == ("conflict", "cycle payment already exists")
    err = await _refused(_invoke(factory, a["admin"], "cycle_payments_upsert", args))
    assert err.code == "no_change"


@pytest.mark.parametrize("tool", ["cycle_payments_create", "cycle_payments_upsert"])
async def test_a_cycle_payment_is_written_and_read_back(factory, w, tool):
    a = w["A"]
    await _cc(factory, a)
    out = await _invoke(factory, a["admin"], tool, {"account_id": a["cc"], **CYCLE, "amount": "80.00"})
    done = await _confirm(factory, a["admin"], out["action_id"])
    assert done["result"] == {"year": CYCLE_YEAR, "month": 6, "amount": "80.00"}
    async with factory() as db:
        [row] = (await db.scalars(select(CcCyclePayment))).all()
    assert (row.account_id, row.amount) == (a["cc"], Decimal("80.00"))


async def test_the_audit_rows_carry_no_client_ip(factory, w):
    """GUARD (TBD-665). The handler's own audit row is written on the tool's
    engine, with no client IP: an in-process call has none."""
    from app.models.audit_event import AuditEvent

    a = w["A"]
    await _cc(factory, a)
    await _cycle_paid(factory, a)
    out = await _invoke(factory, a["admin"], "cycle_payments_delete", {"account_id": a["cc"], **CYCLE})
    await _confirm(factory, a["admin"], out["action_id"])
    async with factory() as db:
        ev = await db.scalar(select(AuditEvent).where(AuditEvent.event_type == "account.cycle_payment.deleted"))
    assert ev is not None and ev.ip_address is None and ev.detail["amount"] == "80.00"
