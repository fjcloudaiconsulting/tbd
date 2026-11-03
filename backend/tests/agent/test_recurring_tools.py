"""TBD-667: the recurring-template and billing-period tools.

The honesty fence (``test_agent_honesty``, T-A9) checks every row each one
writes against its preview, and F-A3 / F1 cover ``recurring_update``'s
disclosure and inverse; this file holds their refusals, their results, and the
two ``route_tool`` changes they needed.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

from sqlalchemy import select

from app.models.audit_event import AuditEvent
from app.models.billing import BillingPeriod
from app.models.recurring import RecurringTransaction
from app.models.transaction import Transaction

from tests.agent.test_actions import (  # noqa: F401  (fixtures)
    P_START, _confirm, _invoke, _refused, engine, factory, w,
)
from tests.agent.test_agent_honesty import TODAY, _occurrence, _series, _series_matched, _template
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
    err = await _refused(_invoke(factory, a["member"], "recurring_update",
                                 {"recurring_id": a["rec"], "next_due_date": (TODAY + 50 * DAY).isoformat()}))
    assert err.code == "conflict"


async def test_generate_with_nothing_due_is_no_change(factory, w):
    err = await _refused(_invoke(factory, w["A"]["member"], "recurring_generate", {}))
    assert err.code == "no_change"


# ── results ───────────────────────────────────────────────────────────────

async def test_stop_reports_and_discloses_the_demoted_row(factory, w):
    a = w["A"]
    await _series_matched(factory, a)
    out, done = await _apply(factory, a["member"], "recurring_stop", {"recurring_id": a["rec"]})
    named = [c["id"] for c in out["changes"] if c["field"] == "reconciliation_state"]
    assert done["result"]["demoted_ids"] == named and len(named) == 1
    assert done["result"]["pending_removed"] == 3
    assert any("marked rejected" in x for x in out["warnings"])


async def test_update_mirrors_onto_siblings_at_the_old_amount_only(factory, w):
    a = w["A"]
    await _series(factory, a)
    out, done = await _apply(factory, a["member"], "recurring_update",
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
    [rid] = [None]
    async with factory() as db:
        t = _template(a, auto_settle=True, next_due_date=P_START, amount=Decimal("20.00"))
        db.add(t)
        await db.commit()
        rid = t.id
    await _add(factory, _occurrence(a, rid, P_START - 5 * DAY, amount=Decimal("20.00")))
    _, done = await _apply(factory, a["member"], "recurring_generate", {})
    assert (done["result"]["generated"], done["result"]["settled"]) == (1, 2)
