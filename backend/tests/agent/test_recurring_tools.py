"""TBD-667: the recurring-template and billing-period tools.

The honesty fence (``test_agent_honesty``, T-A9) checks every row each one
writes against its preview, and F-A3 / F1 cover ``recurring_update``'s
disclosure and inverse; this file holds their refusals, their results, and the
two ``route_tool`` changes they needed.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from sqlalchemy import select

from app.models import Account
from app.models.audit_event import AuditEvent
from app.models.billing import BillingPeriod
from app.models.recurring import RecurringTransaction
from app.models.transaction import Transaction

from tests.agent.test_actions import (  # noqa: F401  (fixtures)
    AUTO, P_START, _confirm, _invoke, _refused, engine, factory, w,
)
from tests.agent.test_agent_honesty import (
    TODAY, _cc, _ids, _occurrence, _savings, _series, _series_matched, _template, _transfer_due,
    _transfer_half_settled, _transfer_pending, _transfer_series,
)
from tests.agent.test_agent_routes import _as, _reset_limiter, client  # noqa: F401
from tests.agent.test_set_category import _add

DAY = datetime.timedelta(days=1)


async def _apply(f, uid, tool, args):
    out = await _invoke(f, uid, tool, args)
    return out, await _confirm(f, uid, out["action_id"])


async def _periods(f, org) -> list[tuple]:
    async with f() as db:
        return [(p.start_date, p.end_date) for p in (await db.scalars(
            select(BillingPeriod).where(BillingPeriod.org_id == org).order_by(BillingPeriod.start_date)
        )).all()]


# ── route_tool ────────────────────────────────────────────────────────────

async def test_an_omitted_query_field_takes_the_routes_default(factory, w):
    """FENCE. Wrong implementation: an omitted query field handed to the
    handler as the args model's None, so ``ensure-future`` with no count
    raised in ``min(max(None, 1), 6)`` instead of creating three stubs."""
    a = w["A"]
    out, done = await _apply(factory, a["admin"], "billing_periods_ensure_future", {})
    assert len(done["result"]) == 3 == len(out["changes"]) // 2
    assert len(await _periods(factory, a["org"])) == 4


async def test_a_route_writing_its_own_audit_row_does_so_from_the_agent(factory, w):
    """FENCE. The billing-cycle handler's audit row (``get_session_factory``)
    is written when an agent action runs it, without a client address."""
    a = w["A"]
    await _apply(factory, a["admin"], "billing_cycle_update", {"billing_cycle_day": 15})
    async with factory() as db:
        row = await db.scalar(select(AuditEvent).where(AuditEvent.event_type == "org.billing_cycle_day.updated"))
    assert row.outcome == "success" and row.ip_address is None
    assert row.detail["new_day"] == 15


# ── refusals ──────────────────────────────────────────────────────────────

async def test_billing_writes_are_admin_only(factory, w):
    a = w["A"]
    for tool, args in (("billing_cycle_update", {"billing_cycle_day": 15}),
                       ("billing_period_close", {}), ("billing_periods_ensure_future", {}),
                       ("billing_period_create", {"start_date": P_START.isoformat()})):
        err = await _refused(_invoke(factory, a["member"], tool, args))
        assert (err.code, err.detail) == ("insufficient_role", "admin"), tool


async def test_a_route_that_would_invent_the_open_period_is_refused(factory, w):
    """Wrong implementation: a preview on an org with no open period, whose
    route then auto-creates one the preview never listed."""
    a = w["A"]
    async with factory() as db:
        p = await db.scalar(select(BillingPeriod).where(BillingPeriod.org_id == a["org"]))
        p.end_date = TODAY
        await db.commit()
    for tool, args in (("billing_cycle_update", {"billing_cycle_day": 15}),
                       ("billing_period_close", {}), ("billing_periods_ensure_future", {})):
        err = await _refused(_invoke(factory, a["admin"], tool, args))
        assert err.code == "no_open_period", tool


async def test_a_close_in_the_future_is_refused_at_preview(factory, w):
    err = await _refused(_invoke(factory, w["A"]["admin"], "billing_period_close",
                                 {"close_date": (TODAY + DAY).isoformat()}))
    assert err.code == "invalid_arguments"


async def test_materialise_refuses_a_stale_occurrence_date(factory, w):
    a = w["A"]
    await _series(factory, a)
    err = await _refused(_invoke(factory, a["member"], "recurring_materialise_next",
                                 {"recurring_id": a["rec"], "occurrence_date": TODAY.isoformat()}))
    assert err.code == "conflict"


async def test_update_refuses_a_next_due_date_onto_an_existing_occurrence(factory, w):
    """The service's own frontier check runs at preview, as a ``conflict``."""
    a = w["A"]
    await _series(factory, a)
    err = await _refused(_invoke(factory, a["member"], "recurring_update_money",
                                 {"recurring_id": a["rec"], "next_due_date": (TODAY + 50 * DAY).isoformat()}))
    assert err.code == "conflict"


async def test_generate_with_nothing_due_is_no_change(factory, w):
    err = await _refused(_invoke(factory, w["A"]["member"], "recurring_generate", {}))
    assert err.code == "no_change"


# ── the write / sensitive split of PUT /recurring/{id} ────────────────────

async def _template_row(f, rid) -> RecurringTransaction:
    async with f() as db:
        return await db.get(RecurringTransaction, rid)


async def test_auto_cannot_change_what_generation_settles(factory, w):
    """FENCE (TBD-667 review). Wrong implementation: amount or auto_settle on
    the ``write`` tool, so an ``agent:auto`` token raises the amount and turns
    on auto-settle with no person confirming, and the scheduled generation
    settles it into the balance. The write tool refuses those fields; the
    money tool only stages, and over MCP only the person confirms it."""
    a = w["A"]
    await _series(factory, a)
    kw = {"api_token_id": a["t1"], **AUTO}
    for money in ({"amount": "5000.00"}, {"auto_settle": True}, {"account_id": a["acct"]},
                  {"type": "income"}, {"occurrence_count": None}, {"next_due_date": TODAY.isoformat()},
                  {"frequency": "weekly"}):
        err = await _refused(_invoke(factory, a["member"], "recurring_update",
                                     {"recurring_id": a["rec"], **money}, **kw))
        assert err.code == "invalid_arguments", money
    out = await _invoke(factory, a["member"], "recurring_update_money",
                        {"recurring_id": a["rec"], "amount": "5000.00", "auto_settle": True}, **kw)
    assert out["requires_browser_confirmation"] is True and "status" not in out
    err = await _refused(_confirm(factory, a["member"], out["action_id"], **kw))
    assert err.code == "requires_browser_confirmation"
    r = await _template_row(factory, a["rec"])
    assert (str(r.amount), r.auto_settle) == ("9.99", False)


async def test_only_the_sensitive_tool_resumes_and_only_the_write_pauses(factory, w):
    a = w["A"]
    await _series(factory, a)
    err = await _refused(_invoke(factory, a["member"], "recurring_update_money",
                                 {"recurring_id": a["rec"], "is_active": False}))
    assert (err.code, err.detail) == ("invalid_arguments", "this tool cannot pause a template; use recurring_update")
    await _apply(factory, a["member"], "recurring_update", {"recurring_id": a["rec"], "is_active": False})
    assert (await _template_row(factory, a["rec"])).is_active is False
    err = await _refused(_invoke(factory, a["member"], "recurring_update",
                                 {"recurring_id": a["rec"], "is_active": True}))
    assert err.detail == "this tool cannot resume a template; use recurring_update_money"
    await _apply(factory, a["member"], "recurring_update_money", {"recurring_id": a["rec"], "is_active": True})
    assert (await _template_row(factory, a["rec"])).is_active is True


# ── results ───────────────────────────────────────────────────────────────

async def test_stop_reports_and_discloses_the_demoted_row(factory, w):
    a = w["A"]
    await _series_matched(factory, a)
    out, done = await _apply(factory, a["member"], "recurring_stop", {"recurring_id": a["rec"]})
    named = [c["id"] for c in out["changes"] if c["field"] == "reconciliation_state"]
    assert done["result"]["demoted_ids"] == named and len(named) == 1
    assert done["result"]["pending_removed"] == 3
    assert any("marked rejected" in x for x in out["warnings"])


async def test_update_money_mirrors_onto_siblings_at_the_old_amount_only(factory, w):
    a = w["A"]
    await _series(factory, a)
    out, done = await _apply(factory, a["member"], "recurring_update_money",
                             {"recurring_id": a["rec"], "amount": "11.00"})
    assert done["result"]["amount"] == "11.00"
    async with factory() as db:
        rows = (await db.scalars(select(Transaction).where(Transaction.recurring_id == a["rec"])
                                 .order_by(Transaction.date))).all()
    assert [str(r.amount) for r in rows] == ["9.99", "11.00", "11.00", "12.00"]
    assert sum(1 for c in out["changes"] if c["entity"] == "transactions") == 2


async def test_skip_next_writes_a_skipped_row_and_moves_the_frontier(factory, w):
    a = w["A"]
    await _series(factory, a)
    due = TODAY + 100 * DAY
    _, done = await _apply(factory, a["member"], "recurring_skip_next",
                           {"recurring_id": a["rec"], "occurrence_date": due.isoformat()})
    assert done["result"]["date"] == due.isoformat()
    async with factory() as db:
        r = await db.get(RecurringTransaction, a["rec"])
        tx = await db.get(Transaction, done["result"]["id"])
    assert r.next_due_date > due and r.occurrences_elapsed == 1
    assert (tx.status.value, tx.reconciliation_state) == ("pending", "skipped")


async def test_generate_settles_and_writes_what_is_due(factory, w):
    a = w["A"]
    async with factory() as db:
        t = _template(a, auto_settle=True, next_due_date=P_START, amount=Decimal("20.00"))
        db.add(t)
        await db.commit()
        rid = t.id
    await _add(factory, _occurrence(a, rid, P_START - 5 * DAY, amount=Decimal("20.00")))
    _, done = await _apply(factory, a["member"], "recurring_generate", {})
    assert (done["result"]["generated"], done["result"]["settled"]) == (1, 2)


# ── recurring transfers (TBD-600) ─────────────────────────────────────────

def _transfer_args(a, **over):
    return {"account_id": a["acct"], "category_id": a["transfer_cat"], "description": "Savings",
            "amount": "25.00", "type": "transfer", "frequency": "monthly",
            "next_due_date": P_START.isoformat(), **over}


async def test_a_transfer_with_no_destination_is_refused_at_preview(factory, w):
    a = w["A"]
    await _savings(factory, a)
    err = await _refused(_invoke(factory, a["member"], "recurring_create", _transfer_args(a)))
    assert (err.code, err.detail) == ("invalid_arguments", "A transfer needs a destination account")
    err = await _refused(_invoke(factory, a["member"], "recurring_create", _transfer_args(
        a, type="expense", category_id=a["c1"], to_account_id=a["savings"])))
    assert err.detail == "Only a transfer has a destination account"


async def test_a_card_is_refused_as_transfer_source_and_accepted_as_destination(factory, w):
    """TBD-789, at preview: a card source is refused on create and on an
    update; a card destination is accepted on both, as the routes accept it."""
    a = w["A"]
    await _savings(factory, a)
    await _cc(factory, a)
    err = await _refused(_invoke(factory, a["member"], "recurring_create", _transfer_args(
        a, account_id=a["cc"], to_account_id=a["savings"])))
    assert err.code == "invalid_arguments" and "credit card or loan" in err.detail
    out = await _invoke(factory, a["member"], "recurring_create", _transfer_args(a, to_account_id=a["cc"]))
    assert out["action_id"]
    async with factory() as db:
        db.add(_template(a, type="transfer", to_account_id=a["savings"], category_id=a["transfer_cat"]))
        await db.commit()
        rid = (await db.scalars(select(RecurringTransaction.id))).one()
    err = await _refused(_invoke(factory, a["member"], "recurring_update_money",
                                 {"recurring_id": rid, "account_id": a["cc"]}))
    assert "credit card or loan" in err.detail
    out = await _invoke(factory, a["member"], "recurring_update_money",
                        {"recurring_id": rid, "to_account_id": a["cc"]})
    assert out["action_id"]


async def test_a_transfer_cannot_change_type(factory, w):
    a = w["A"]
    await _transfer_series(factory, a)
    err = await _refused(_invoke(factory, a["member"], "recurring_update_money",
                                 {"recurring_id": a["rec"], "type": "expense", "category_id": a["c1"]}))
    assert err.detail == "A recurring transfer cannot change type. Stop it and create a new series."
    err = await _refused(_invoke(factory, a["member"], "recurring_update", {"recurring_id": a["rec"],
                                                                           "to_account_id": a["acct"]}))
    assert err.code == "invalid_arguments"  # the destination is a money field


async def test_stop_warns_about_a_kept_half_settled_pair(factory, w):
    a = w["A"]
    await _transfer_half_settled(factory, a)
    out, done = await _apply(factory, a["member"], "recurring_stop", {"recurring_id": a["rec"]})
    assert "1 pending occurrence(s) are deleted." in out["warnings"]
    assert any("1 pending transfer leg(s) are kept" in x for x in out["warnings"])
    assert done["result"]["pending_removed"] == 2
    async with factory() as db:
        kept = await db.get(Transaction, a["kept_inc"])
    assert (kept.status.value, kept.linked_transaction_id) == ("pending", a["kept_exp"])


async def test_a_transfer_source_cannot_become_a_card_or_an_account_it_uses_be_deleted(factory, w):
    """The refusals the account routes make (TBD-600/789), at the money tool's
    and the delete's preview. The destination may become a card."""
    a = w["A"]
    await _transfer_series(factory, a)
    await _cc(factory, a)
    err = await _refused(_invoke(factory, a["member"], "accounts_update_money",
                                 {"account_id": a["acct"], "account_type_id": a["cc_type"],
                                  "close_day": 20}))
    assert err.code == "invalid_arguments" and "source of a recurring transfer" in err.detail
    out = await _invoke(factory, a["member"], "accounts_update_money",
                        {"account_id": a["savings"], "account_type_id": a["cc_type"], "close_day": 20})
    assert out["action_id"]
    async with factory() as db:
        acct = await db.get(Account, a["savings"])
        acct.balance = Decimal("0.00")
        await db.commit()
    err = await _refused(_invoke(factory, a["member"], "accounts_delete", {"account_id": a["savings"]}))
    assert err.code == "conflict" and "recurring transaction" in err.detail


async def test_generate_counts_a_transfer_pair_as_one_occurrence(factory, w):
    """Wrong implementation: counting legs, so one transfer reads as two."""
    a = w["A"]
    await _transfer_due(factory, a)
    out, done = await _apply(factory, a["member"], "recurring_generate", {})
    assert out["summary"].startswith("Write 1 recurring occurrence(s) due by ")
    assert out["summary"].endswith(" and settle 1 due one(s)")
    assert done["result"]["generated"] == 1


async def test_the_update_warning_counts_a_transfer_pair_once(factory, w):
    a = w["A"]
    await _transfer_pending(factory, a)
    out = await _invoke(factory, a["member"], "recurring_update_money", {"recurring_id": a["rec"], "amount": "30.00"})
    assert sum(1 for c in out["changes"] if c["entity"] == "transactions") == 2
    assert "Also updates 1 pending occurrence(s) that still had the old value." in out["warnings"]


@pytest.mark.parametrize("side", ["source", "destination"])
async def test_an_account_a_recurring_template_uses_is_not_deleted(factory, w, client, side):
    """FENCE (TBD-600 review). Neither recurring foreign key has ON DELETE, so
    the route used to fail in the database. REST answers 409 and the agent's
    preview refuses with the same message."""
    a = w["A"]
    await _savings(factory, a)
    async with factory() as db:
        type_id = (await db.get(Account, a["acct"])).account_type_id
    src, dst = await _ids(factory, *(Account(org_id=a["org"], name=n, account_type_id=type_id,
                                              balance=Decimal("0.00"), currency="EUR", is_default=False)
                                      for n in ("From", "To")))
    await _add(factory, _template(a, type="transfer", account_id=src, to_account_id=dst,
                                  category_id=a["transfer_cat"]))
    target = src if side == "source" else dst
    _as(factory, a["member"])
    r = await client.delete(f"/api/v1/accounts/{target}")
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "Cannot delete: 1 recurring transaction(s) use this account"
    err = await _refused(_invoke(factory, a["member"], "accounts_delete", {"account_id": target}))
    assert (err.code, err.detail) == ("conflict", r.json()["detail"])
    async with factory() as db:
        assert await db.get(Account, target) is not None
