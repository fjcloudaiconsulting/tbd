"""TBD-664 A2: the delete side-effect pass split into a plan and an apply.

``_settle_batch_counters_and_demote_orphans`` used to decide AND write in one
pass, so nothing could say what a delete would do without doing it. An agent
preview has to (it lists every row a confirm changes), so the decision is now
``plan_batch_settlement`` (pure: no SQL, no ORM mutation) and the writes are
``apply_batch_settlement``; ``delete_transaction`` is likewise
``plan_transaction_delete`` + ``apply_transaction_delete``.

This file is the CHARACTERIZATION fence for that refactor. Each scenario's
golden (the full database diff, the return value and the forensic log lines)
was recorded from the pre-refactor code and must stay byte-identical:

* ``test_char_*``: every delete path (single, bulk, recurring stop/delete) and
  the helper's unlocked-id branch, against its golden;
* ``test_plan_*``: the plan writes nothing, and predicts exactly what the apply
  then does (demoted ids, counter movement, deleted rows, balance reverts).
"""
from __future__ import annotations

import datetime
import os
from decimal import Decimal

import pytest
from sqlalchemy import event, select

from app.models import ImportBatch, Transaction
from app.models.base import Base
from app.models.transaction import TransactionStatus, TransactionType
from app.schemas.import_reconciliation import ReconciliationState
from app.services import recurring_service, transaction_service
from tests.services.test_matched_row_actions import (  # noqa: F401  (db_session fixture)
    _batch_of_three,
    _create,
    _make_matched_pair,
    _make_recurring_pending,
    _reconcile,
    _seed,
    _transition,
    db_session,
)

# Events the delete paths emit, and the fields of each that a golden pins.
LOGGED = {
    "transactions.match_orphan_rejected": (
        "deleted_transaction_id", "demoted_transaction_id", "prior_state", "amount", "tx_type",
    ),
    "transactions.delete_counter_skipped_unlocked_row": ("transaction_id",),
    "import.batch_auto_closed": ("batch_id", "row_count"),
    "import.batch_pending_count_drift": ("batch_id", "counter_value", "actual"),
}
# Clock columns: a golden pins only whether they were stamped. ``updated_at``
# moves only when a write crosses a second boundary, so it is not compared.
STAMPED = {("import_batches", "closed_at")}


# ── observation ───────────────────────────────────────────────────────────

async def _snap(db) -> dict:
    conn = await db.connection()
    out = {}
    for t in Base.metadata.sorted_tables:
        pk = [c.name for c in t.primary_key.columns]
        rows = (await conn.execute(select(t))).mappings().all()
        out[t.name] = {tuple(r[p] for p in pk): dict(r) for r in rows}
    return out


class Names:
    """Fixture ids rendered as labels, so a golden reads as the scenario."""

    def __init__(self, seed: dict, **rows: int) -> None:
        self.ids = {
            ("accounts", seed["acct_a_id"]): "acct_a", ("accounts", seed["acct_b_id"]): "acct_b",
            ("accounts", seed["acct_c_id"]): "acct_c", ("import_batches", seed["batch_id"]): "batch",
        }
        self.ids.update({("transactions", v): k for k, v in rows.items()})
        self.tx = {v: k for k, v in rows.items()}
        self.batch = seed["batch_id"]

    def row(self, table: str, pk: tuple) -> str:
        return self.ids.get((table, pk[0]), f"{table}:{pk[0]}") if len(pk) == 1 else f"{table}:{pk}"

    def value(self, col: str, v):
        if col in ("linked_transaction_id", "deleted_transaction_id", "demoted_transaction_id",
                   "transaction_id") and v in self.tx:
            return self.tx[v]
        if col == "batch_id" and v == self.batch:
            return "batch"
        if isinstance(v, Decimal):
            return str(v)
        if isinstance(v, (datetime.date, datetime.datetime)):
            return v.isoformat()
        return getattr(v, "value", v)


def _diff(pre: dict, post: dict, names: Names) -> dict:
    out = {}
    for table in pre:
        if table == "audit_events":
            continue
        for pk in pre[table].keys() - post[table].keys():
            out[f"-{names.row(table, pk)}"] = None
        for pk in post[table].keys() - pre[table].keys():
            out[f"+{names.row(table, pk)}"] = None
        for pk in pre[table].keys() & post[table].keys():
            a, b = pre[table][pk], post[table][pk]
            cols = {}
            for c in a:
                if a[c] == b[c] or c == "updated_at":
                    continue
                if (table, c) in STAMPED:
                    cols[c] = "stamped" if b[c] is not None else None
                else:
                    cols[c] = (names.value(c, a[c]), names.value(c, b[c]))
            if cols:
                out[names.row(table, pk)] = cols
    return out


def _events(lines: list[dict], names: Names) -> list[tuple]:
    return [
        (ln["msg"], *(names.value(f, ln.get(f)) for f in LOGGED[ln["msg"]]))
        for ln in lines if ln.get("msg") in LOGGED
    ]


async def _observe(db, log_lines, names: Names, action) -> dict:
    pre = await _snap(db)
    skip = len(log_lines())
    result = await action()
    db.expire_all()
    post = await _snap(db)
    return {
        "result": result,
        "diff": _diff(pre, post, names),
        "logs": _events(log_lines()[skip:], names),
    }


def _check(observed: dict, golden: dict) -> None:
    if os.environ.get("CHAR_PRINT"):
        print("\nOBSERVED", repr(observed))
    assert observed == golden


# ── scenarios ─────────────────────────────────────────────────────────────

def _ids(names: Names, ids) -> list[str]:
    return [names.tx[i] for i in ids]


async def test_char_delete_last_pending_batch_row_closes_the_batch(db_session, log_lines):
    seed = await _seed(db_session)
    rows = await _batch_of_three(db_session, seed)
    names = Names(seed, r0=rows[0].id, r1=rows[1].id, r2=rows[2].id)

    async def act():
        return _ids(names, await transaction_service.delete_transaction(
            db_session, seed["org_id"], rows[2].id))

    _check(await _observe(db_session, log_lines, names, act), {
        "result": [],
        "diff": {
            "-r2": None,
            "acct_a": {"balance": ("944.00", "976.00")},
            "batch": {
                "row_count": (3, 2), "pending_count": (1, 0), "status": ("open", "closed"),
                "closed_at": "stamped",
            },
        },
        "logs": [("import.batch_auto_closed", "batch", 2)],
    })


async def test_char_delete_canonical_demotes_its_matched_duplicate(db_session, log_lines):
    seed = await _seed(db_session)
    dup, canonical = await _make_matched_pair(db_session, seed, amount="256.00")
    names = Names(seed, dup=dup.id, canonical=canonical.id)

    async def act():
        return _ids(names, await transaction_service.delete_transaction(
            db_session, seed["org_id"], canonical.id))

    _check(await _observe(db_session, log_lines, names, act), {
        "result": ["dup"],
        "diff": {
            "-canonical": None,
            "acct_b": {"balance": ("144.00", "400.00")},
            "dup": {
                "reconciliation_state": ("matched", "rejected"),
                "linked_transaction_id": ("canonical", None),
            },
        },
        "logs": [(
            "transactions.match_orphan_rejected", "canonical", "dup", "matched", "256.00", "expense",
        )],
    })


async def test_char_delete_demotes_a_pending_review_referrer_and_closes(db_session, log_lines):
    seed = await _seed(db_session)
    filler = await _create(db_session, seed, account_id=seed["acct_a_id"], amount="1.00", in_batch=True)
    dup, canonical = await _make_matched_pair(db_session, seed, amount="64.00")
    await _reconcile(db_session, seed, _transition(dup.id, ReconciliationState.ACCEPTED))
    await _reconcile(db_session, seed, _transition(dup.id, ReconciliationState.PENDING_REVIEW))
    await _reconcile(db_session, seed, _transition(filler.id, ReconciliationState.ACCEPTED))
    names = Names(seed, filler=filler.id, dup=dup.id, canonical=canonical.id)

    async def act():
        return _ids(names, await transaction_service.delete_transaction(
            db_session, seed["org_id"], canonical.id))

    _check(await _observe(db_session, log_lines, names, act), {
        "result": ["dup"],
        "diff": {
            "-canonical": None,
            "acct_b": {"balance": ("336.00", "400.00")},
            "dup": {
                "reconciliation_state": ("pending_review", "rejected"),
                "linked_transaction_id": ("canonical", None),
            },
            "batch": {"pending_count": (1, 0), "status": ("open", "closed"), "closed_at": "stamped"},
        },
        "logs": [
            ("transactions.match_orphan_rejected", "canonical", "dup", "pending_review", "64.00", "expense"),
            ("import.batch_auto_closed", "batch", 2),
        ],
    })


async def test_char_delete_one_transfer_leg_demotes_a_referrer_of_the_other(db_session, log_lines):
    seed = await _seed(db_session)
    expense = await _create(db_session, seed, account_id=seed["acct_a_id"], amount="16.00",
                            tx_type=TransactionType.EXPENSE, label="leg-e")
    income = await _create(db_session, seed, account_id=seed["acct_b_id"], amount="16.00",
                           tx_type=TransactionType.INCOME, label="leg-i")
    await transaction_service.pair_existing_transactions(
        db_session, seed["org_id"], expense_tx_id=expense.id, income_tx_id=income.id,
        recategorize=False,
    )
    dup, _ = await _make_matched_pair(db_session, seed, amount="16.00", canonical=income)
    await _reconcile(db_session, seed, _transition(dup.id, ReconciliationState.ACCEPTED))
    names = Names(seed, expense=expense.id, income=income.id, dup=dup.id)

    async def act():
        return _ids(names, await transaction_service.delete_transaction(
            db_session, seed["org_id"], expense.id))

    _check(await _observe(db_session, log_lines, names, act), {
        "result": ["dup"],
        "diff": {
            "-expense": None,
            "-income": None,
            "acct_a": {"balance": ("984.00", "1000.00")},
            "acct_b": {"balance": ("416.00", "400.00")},
            "dup": {
                "reconciliation_state": ("accepted", "rejected"),
                "linked_transaction_id": ("income", None),
            },
            "batch": {"accepted_count": (1, 0)},
        },
        "logs": [("transactions.match_orphan_rejected", "income", "dup", "accepted", "16.00", "expense")],
    })


async def test_char_bulk_delete_demotes_and_settles(db_session, log_lines):
    seed = await _seed(db_session)
    rows = await _batch_of_three(db_session, seed)
    dup, canonical = await _make_matched_pair(db_session, seed, amount="128.00")
    names = Names(seed, r0=rows[0].id, r1=rows[1].id, r2=rows[2].id, dup=dup.id, canonical=canonical.id)

    async def act():
        n, skipped, demoted = await transaction_service.bulk_delete_transactions(
            db_session, seed["org_id"], [canonical.id, rows[0].id, rows[2].id])
        return n, _ids(names, skipped), _ids(names, demoted)

    _check(await _observe(db_session, log_lines, names, act), {
        "result": (3, [], ["dup"]),
        "diff": {
            "-canonical": None,
            "-r0": None,
            "-r2": None,
            "acct_a": {"balance": ("944.00", "984.00")},
            "acct_b": {"balance": ("272.00", "400.00")},
            "dup": {
                "reconciliation_state": ("matched", "rejected"),
                "linked_transaction_id": ("canonical", None),
            },
            "batch": {
                "row_count": (4, 2), "accepted_count": (2, 1), "pending_count": (1, 0),
                "status": ("open", "closed"), "closed_at": "stamped",
            },
        },
        "logs": [
            ("transactions.match_orphan_rejected", "canonical", "dup", "matched", "128.00", "expense"),
            ("import.batch_auto_closed", "batch", 2),
        ],
    })


async def test_char_stop_recurring_demotes_the_match_orphan(db_session, log_lines):
    seed = await _seed(db_session)
    rec, pending = await _make_recurring_pending(db_session, seed, amount="1024.00")
    dup, _ = await _make_matched_pair(db_session, seed, amount="1024.00", canonical=pending)
    names = Names(seed, pending=pending.id, dup=dup.id)
    rec_id = rec.id

    async def act():
        out = await recurring_service.stop_recurring(db_session, seed["org_id"], rec_id)
        return out.removed, _ids(names, out.demoted_ids)

    observed = await _observe(db_session, log_lines, names, act)
    observed["diff"].pop(f"recurring_transactions:{rec_id}")  # is_active, the stop itself
    _check(observed, {
        "result": (1, ["dup"]),
        "diff": {
            "-pending": None,
            "dup": {
                "reconciliation_state": ("matched", "rejected"),
                "linked_transaction_id": ("pending", None),
            },
        },
        "logs": [("transactions.match_orphan_rejected", "pending", "dup", "matched", "1024.00", "expense")],
    })


async def test_char_helper_reports_an_unlocked_id_and_still_settles_the_rest(db_session, log_lines):
    seed = await _seed(db_session)
    rows = await _batch_of_three(db_session, seed)
    names = Names(seed, r0=rows[0].id, r1=rows[1].id, r2=rows[2].id)
    locked = {r.id: r for r in (await db_session.scalars(
        select(Transaction).where(Transaction.id.in_([rows[1].id]))
    )).all()}

    async def act():
        return _ids(names, await transaction_service._settle_batch_counters_and_demote_orphans(
            db_session, seed["org_id"], locked_rows=locked, deleted_ids={rows[1].id, rows[2].id}))

    _check(await _observe(db_session, log_lines, names, act), {
        "result": [],
        "diff": {"batch": {"row_count": (3, 2), "accepted_count": (2, 1)}},
        "logs": [("transactions.delete_counter_skipped_unlocked_row", "r2")],
    })


# ── the plan ──────────────────────────────────────────────────────────────

async def _demotion_world(db):
    seed = await _seed(db)
    filler = await _create(db, seed, account_id=seed["acct_a_id"], amount="1.00", in_batch=True)
    dup, canonical = await _make_matched_pair(db, seed, amount="64.00")
    await _reconcile(db, seed, _transition(dup.id, ReconciliationState.ACCEPTED))
    await _reconcile(db, seed, _transition(dup.id, ReconciliationState.PENDING_REVIEW))
    return seed, filler, dup, canonical


async def test_plan_writes_nothing_and_issues_no_sql(db_session):
    """Wrong implementation: a plan that flushes, mutates a loaded row (the
    state write moved into it), or reads the database."""
    seed, filler, dup, canonical = await _demotion_world(db_session)
    rows = {r.id: r for r in (await db_session.scalars(select(Transaction))).all()}
    states = {i: r.reconciliation_state for i, r in rows.items()}
    statements = []
    engine = db_session.bind.sync_engine

    def _count(*a):
        statements.append(a[2])

    event.listen(engine, "before_cursor_execute", _count)
    try:
        plan = transaction_service.plan_batch_settlement(
            seed["org_id"], locked_rows=rows, deleted_ids={canonical.id, filler.id})
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    assert statements == []
    assert not db_session.dirty and not db_session.new and not db_session.deleted
    assert {i: r.reconciliation_state for i, r in rows.items()} == states
    assert plan.demoted_ids == [dup.id]
    assert plan.prior_states == {dup.id: "pending_review"}


async def test_plan_predicts_what_apply_writes(db_session):
    """Wrong implementation: an apply that recomputes instead of applying the
    plan, or a plan whose counter deltas differ from the movement."""
    seed, filler, dup, canonical = await _demotion_world(db_session)
    batch = await db_session.get(ImportBatch, seed["batch_id"])
    before = (batch.row_count, batch.accepted_count, batch.pending_count)

    plan = await transaction_service.plan_transaction_delete(db_session, seed["org_id"], canonical.id)
    assert sorted(plan.deleted) == [canonical.id]
    assert [(r.account_id, r.amount, r.type) for r in plan.reverts] == [
        (seed["acct_b_id"], Decimal("64.00"), TransactionType.EXPENSE)]
    s = plan.settlement
    assert s.demoted_ids == [dup.id]
    assert (s.row_delta, s.accepted_delta, s.pending_delta) == ({}, {}, {seed["batch_id"]: 1})
    assert s.unlocked_ids == []

    assert await transaction_service.apply_transaction_delete(db_session, seed["org_id"], plan) == [dup.id]
    batch = await db_session.scalar(
        select(ImportBatch).where(ImportBatch.id == seed["batch_id"])
        .execution_options(populate_existing=True))
    assert (batch.row_count, batch.accepted_count, batch.pending_count) == (
        before[0], before[1], before[2] - 1)
    assert await db_session.get(Transaction, canonical.id) is None


async def test_plan_skips_rows_already_reverted_and_reciprocal_partners(db_session):
    """The plan keeps the helper's three exclusions: a referrer in the delete
    set, one already in a reverted state, and one inside the cached balance."""
    seed = await _seed(db_session)
    expense = await _create(db_session, seed, account_id=seed["acct_a_id"], amount="16.00",
                            tx_type=TransactionType.EXPENSE, label="leg-e")
    income = await _create(db_session, seed, account_id=seed["acct_b_id"], amount="16.00",
                           tx_type=TransactionType.INCOME, label="leg-i")
    await transaction_service.pair_existing_transactions(
        db_session, seed["org_id"], expense_tx_id=expense.id, income_tx_id=income.id,
        recategorize=False,
    )
    dup, _ = await _make_matched_pair(db_session, seed, amount="16.00", canonical=income)
    for state in ("ACCEPTED", "PENDING_REVIEW", "REJECTED"):
        await _reconcile(db_session, seed, _transition(dup.id, ReconciliationState[state]))
    rows = {r.id: r for r in (await db_session.scalars(select(Transaction))).all()}

    plan = transaction_service.plan_batch_settlement(
        seed["org_id"], locked_rows=rows, deleted_ids={income.id})
    assert plan.demoted_ids == []  # expense is reciprocal; dup is already rejected

    plan = await transaction_service.plan_transaction_delete(db_session, seed["org_id"], expense.id)
    assert sorted(plan.deleted) == sorted([expense.id, income.id])
    assert plan.settlement.demoted_ids == []
    assert rows[dup.id].status == TransactionStatus.SETTLED
