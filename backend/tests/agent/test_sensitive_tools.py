"""TBD-664 PR B: the sensitive transaction tools and the browser-only confirm.

``transactions_update_money``, ``transactions_create``, ``transactions_delete``
and ``transactions_transfer`` move money, so they are ``sensitive``. The
honesty fence (``test_agent_honesty``, T-A9) checks every row each one writes
against its preview; this file holds their refusals, their results, and the
security property the tier exists for: an agent stages, only the person
confirms, in the app.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import Account
from app.models.agent_pending_action import AgentPendingAction
from app.models.category import Category, CategoryType
from app.models.recurring import Frequency, RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus

from tests.agent.test_actions import (  # noqa: F401  (fixtures)
    AUTO, MCP, P_START, _confirm, _invoke, _refused, engine, factory, w,
)
from tests.agent.test_agent_honesty import _matched_import_rows, _savings
from tests.agent.test_set_category import _add, _make_pair, _set, _tx


async def _balance(f, account_id) -> Decimal:
    async with f() as db:
        return (await db.get(Account, account_id)).balance


async def _apply(f, a, tool, args):
    out = await _invoke(f, a["member"], tool, args)
    return out, await _confirm(f, a["member"], out["action_id"])


# ── the browser-only confirm, on a real sensitive tool ────────────────────

@pytest.mark.parametrize("scope", ["agent:write", "agent:auto"])
async def test_an_agent_stages_a_delete_but_only_the_person_confirms_it(factory, w, scope):
    """FENCE (TBD-664, the MCP self-confirm hole, shipped with the first
    sensitive tool). Wrong implementation: ``confirm_action`` over MCP running
    a sensitive action, so an injected model deletes a transaction (or moves
    money) end to end. The row stays pending for the person."""
    a = w["A"]
    kw = {"channel": "mcp", "scope": scope, "api_token_id": a["t1"]}
    out = await _invoke(factory, a["member"], "transactions_delete", {"transaction_id": a["tx"]}, **kw)
    assert out["requires_confirmation"] is True and out["requires_browser_confirmation"] is True
    err = await _refused(_confirm(factory, a["member"], out["action_id"], **kw))
    assert err.code == "requires_browser_confirmation"
    assert await _tx(factory, a["tx"]) is not None
    async with factory() as db:
        row = await db.get(AgentPendingAction, out["action_id"])
    assert (row.status.value, row.mode.value, row.channel.value) == ("pending", "confirm", "mcp")
    done = await _confirm(factory, a["member"], out["action_id"])
    assert done["status"] == "done" and done["result"] == {"deleted": True, "demoted_ids": []}
    assert await _tx(factory, a["tx"]) is None


# ── transactions_delete ───────────────────────────────────────────────────

async def test_delete_reports_the_demoted_ids_and_discloses_them(factory, w):
    a = w["A"]
    await _matched_import_rows(factory, a)
    async with factory() as db:
        refs = {r.reconciliation_state: r.id for r in (await db.scalars(
            select(Transaction).where(Transaction.linked_transaction_id == a["tx"])
        )).all()}
    out, done = await _apply(factory, a, "transactions_delete", {"transaction_id": a["tx"]})
    demoted = sorted([refs["pending_review"], refs["accepted"]])
    assert sorted(done["result"]["demoted_ids"]) == demoted
    named = sorted(c["id"] for c in out["changes"] if c["field"] == "reconciliation_state")
    assert named == demoted
    assert any("marked rejected" in x for x in out["warnings"])
    assert (await _tx(factory, refs["rejected"])).linked_transaction_id is None


async def test_delete_gives_a_settled_amount_back_and_takes_the_other_leg(factory, w):
    a = w["A"]
    partner, _ = await _make_pair(factory, a)
    before = await _balance(factory, a["acct"])
    out, _ = await _apply(factory, a, "transactions_delete", {"transaction_id": a["tx"]})
    assert "other leg" in out["summary"]
    assert {c["id"] for c in out["changes"] if c["field"] == "type"} == {a["tx"], partner}
    # the rows that cease to exist are stated, not inferred from nulls
    assert out["context"]["deleted"] == [
        {"entity": "transactions", "id": i} for i in sorted([a["tx"], partner])]
    assert await _balance(factory, a["acct"]) == before + Decimal("9.99")
    assert await _tx(factory, partner) is None


async def test_delete_refuses_a_manual_adjustment_and_another_orgs_row(factory, w):
    a, b = w["A"], w["B"]
    err = await _refused(_invoke(factory, a["member"], "transactions_delete", {"transaction_id": b["tx"]}))
    assert err.code == "not_found"
    await _set(factory, Transaction, a["tx"], is_manual_adjustment=True)
    err = await _refused(_invoke(factory, a["member"], "transactions_delete", {"transaction_id": a["tx"]}))
    assert err.code == "invalid_arguments"


# ── transactions_update_money ─────────────────────────────────────────────

async def test_money_edit_names_the_balance_move_and_makes_it(factory, w):
    a = w["A"]
    before = await _balance(factory, a["acct"])
    out, _ = await _apply(factory, a, "transactions_update_money",
                          {"transaction_id": a["tx"], "amount": "25.00"})
    assert out["changes"][0] == {"entity": "transactions", "id": a["tx"], "field": "amount",
                                 "before": "9.99", "after": "25.00", "currency": "EUR"}
    assert out["changes"][-1] == {"entity": "accounts", "id": a["acct"], "field": "balance_delta",
                                  "before": None, "after": "-15.01", "currency": "EUR"}
    assert await _balance(factory, a["acct"]) == before - Decimal("15.01")


async def test_money_edit_refuses_what_writes_beyond_the_row(factory, w):
    """FENCE. Wrong implementation: a money edit of a transfer leg (the
    service mirrors the amount onto the partner) or an account move inside a
    recurring series (it moves the template and every pending sibling), none
    of which the templated preview lists."""
    a = w["A"]
    [rid] = await _add(factory, RecurringTransaction(
        org_id=a["org"], account_id=a["acct"], category_id=a["c1"], description="SPOTIFY PREMIUM",
        amount=Decimal("9.99"), type="expense", frequency=Frequency("monthly"),
        next_due_date=P_START + datetime.timedelta(days=40),
    ))
    await _set(factory, Transaction, a["tx"], recurring_id=rid)
    await _savings(factory, a)
    err = await _refused(_invoke(factory, a["member"], "transactions_update_money",
                                 {"transaction_id": a["tx"], "account_id": a["savings"]}))
    assert (err.code, err.data["reason"]) == ("unsupported_in_v1", "recurring_series")
    # the amount of a series row moves nothing else: allowed
    await _apply(factory, a, "transactions_update_money", {"transaction_id": a["tx"], "amount": "11.00"})
    await _make_pair(factory, a)
    err = await _refused(_invoke(factory, a["member"], "transactions_update_money",
                                 {"transaction_id": a["tx"], "amount": "12.00"}))
    assert (err.code, err.data["reason"]) == ("unsupported_in_v1", "linked_transaction")


async def test_money_edit_checks_at_preview_what_the_service_refuses_at_execute(factory, w):
    """FENCE. Wrong implementation: a preview promising a type flip the
    category forbids, or an account that is not the org's."""
    a, b = w["A"], w["B"]
    for args in ({"type": "income"}, {"account_id": b["acct"]}):
        err = await _refused(_invoke(factory, a["member"], "transactions_update_money",
                                     {"transaction_id": a["tx"], **args}))
        assert err.code == "invalid_arguments", args
    err = await _refused(_invoke(factory, a["member"], "transactions_update_money",
                                 {"transaction_id": a["tx"], "amount": "9.99", "status": "settled"}))
    assert err.code == "no_change"


# ── transactions_create ───────────────────────────────────────────────────

async def test_create_returns_the_row_and_refuses_a_category_of_the_wrong_type(factory, w):
    a = w["A"]
    base = {"account_id": a["acct"], "category_id": a["c2"], "description": "Coffee",
            "amount": "4.50", "type": "expense", "date": P_START.isoformat()}
    err = await _refused(_invoke(factory, a["member"], "transactions_create", {**base, "type": "income"}))
    assert err.code == "invalid_arguments"
    out, done = await _apply(factory, a, "transactions_create", base)
    assert {c["id"] for c in out["changes"] if c["entity"] == "transactions"} == {"new:1"}
    assert done["result"]["amount"] == "4.50" and done["result"]["status"] == "settled"
    assert (await _tx(factory, done["result"]["id"])).settled_date == P_START


async def test_create_needs_the_route_required_fields(factory, w):
    a = w["A"]
    err = await _refused(_invoke(factory, a["member"], "transactions_create",
                                 {"account_id": a["acct"], "amount": "1.00"}))
    assert err.code == "invalid_arguments"


# ── transactions_transfer ─────────────────────────────────────────────────

async def test_transfer_links_two_legs_and_moves_both_balances(factory, w):
    a = w["A"]
    await _savings(factory, a)
    src, dst = await _balance(factory, a["acct"]), await _balance(factory, a["savings"])
    out, done = await _apply(factory, a, "transactions_transfer", {
        "from_account_id": a["acct"], "to_account_id": a["savings"], "amount": "30.00",
        "date": P_START.isoformat(),
    })
    assert out["context"]["description"] == {"untrusted": "Transfer from Main A to Savings"}
    expense, income = done["result"]
    assert (expense["linked_transaction_id"], income["linked_transaction_id"]) == (income["id"], expense["id"])
    assert (expense["category_id"], income["category_id"]) == (a["transfer_cat"], a["transfer_cat"])
    assert await _balance(factory, a["acct"]) == src - Decimal("30.00")
    assert await _balance(factory, a["savings"]) == dst + Decimal("30.00")


async def test_transfer_refuses_what_the_service_would_refuse_or_invent(factory, w):
    """FENCE. Wrong implementation: a preview for a transfer the service then
    refuses (same account, two currencies), or one that silently creates the
    Transfer category it never listed."""
    a, b = w["A"], w["B"]
    base = {"from_account_id": a["acct"], "amount": "5.00", "date": P_START.isoformat()}
    err = await _refused(_invoke(factory, a["member"], "transactions_transfer",
                                 {**base, "to_account_id": a["acct"]}))
    assert err.code == "invalid_arguments"
    err = await _refused(_invoke(factory, a["member"], "transactions_transfer",
                                 {**base, "to_account_id": b["acct"]}))
    assert err.code == "invalid_arguments"
    [usd, both] = await _add(
        factory,
        Account(org_id=a["org"], name="Dollars", account_type_id=(await _account_type(factory, a)),
                balance=Decimal("0.00"), currency="USD", is_default=False),
        Category(org_id=a["org"], name="Moves", type=CategoryType.BOTH),
    )
    err = await _refused(_invoke(factory, a["member"], "transactions_transfer",
                                 {**base, "to_account_id": usd}))
    assert err.code == "invalid_arguments"
    [eur] = await _add(factory, Account(
        org_id=a["org"], name="Pot", account_type_id=(await _account_type(factory, a)),
        balance=Decimal("0.00"), currency="EUR", is_default=False,
    ))
    err = await _refused(_invoke(factory, a["member"], "transactions_transfer",
                                 {**base, "to_account_id": eur}))
    assert (err.code, err.data["reason"]) == ("unsupported_in_v1", "no_transfer_category")
    await _apply(factory, a, "transactions_transfer", {**base, "to_account_id": eur, "category_id": both})


async def _account_type(f, a) -> int:
    async with f() as db:
        return (await db.get(Account, a["acct"])).account_type_id


async def test_no_sensitive_tool_runs_on_an_auto_token(factory, w):
    """FENCE F-A1 on the real tools: ``agent:auto`` stages them like any token."""
    a = w["A"]
    out = await _invoke(factory, a["member"], "transactions_update_money",
                        {"transaction_id": a["tx"], "status": "pending"}, api_token_id=a["t1"], **AUTO)
    assert out["requires_confirmation"] is True
    assert (await _tx(factory, a["tx"])).status == TransactionStatus.SETTLED
    out = await _invoke(factory, a["member"], "transactions_delete", {"transaction_id": a["tx"]},
                        api_token_id=a["t1"], **MCP)
    assert out["requires_browser_confirmation"] is True
