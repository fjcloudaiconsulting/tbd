"""TBD-791: the auto-settle sweep honours a pending row's expected settlement.

``_settle_due_auto`` settles an auto-settle occurrence once
``coalesce(settled_date, date) <= today`` and stamps that date, so rent moved
from the 20th to the 24th neither settles nor moves the balance on the 20th.
A transfer pair settles both legs together once both are due. Generation
writes pending occurrences with ``settled_date`` NULL, so an unmoved row
behaves exactly as before.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from app.models.transaction import Transaction, TransactionStatus
from app.schemas.transaction import TransactionUpdate
from app.services import recurring_service, transaction_service

from tests.services.test_recurring_transfers import (  # noqa: F401  (fixtures)
    AMOUNT, TODAY, _balances, _create, _rows, db, seed,
)

DUE = datetime.date(2026, 7, 20)
MOVED = datetime.date(2026, 7, 24)


async def _generated(db, seed, **overrides) -> list[Transaction]:
    await _create(db, seed, next_due_date=DUE, auto_settle=True, **overrides)
    await recurring_service.generate_due_transactions(db, seed["org_id"], today=TODAY)
    rows = await _rows(db, seed["org_id"])
    assert {(t.status, t.date, t.settled_date) for t in rows} == {
        (TransactionStatus.PENDING, DUE, None),
    }
    return rows


async def _sweep(db, seed, day) -> int:
    n = await recurring_service._settle_due_auto(db, seed["org_id"], day)
    await db.commit()
    return n


async def _expense(db, seed) -> Transaction:
    [row] = await _generated(
        db, seed, type="expense", to_account_id=None, category_id=seed["food"],
        description="Rent",
    )
    return row


@pytest.mark.asyncio
async def test_a_moved_row_settles_on_its_expected_date(db, seed):
    row = await _expense(db, seed)
    await transaction_service.update_transaction(
        db, seed["org_id"], row.id, TransactionUpdate(settled_date=MOVED),
    )
    before = await _balances(db, seed)

    for day in (DUE, MOVED - datetime.timedelta(days=1)):
        assert await _sweep(db, seed, day) == 0
        assert await _balances(db, seed) == before
    assert await _sweep(db, seed, MOVED) == 1

    [after] = await _rows(db, seed["org_id"])
    assert (after.status, after.date, after.settled_date) == (
        TransactionStatus.SETTLED, DUE, MOVED,
    )
    assert (await _balances(db, seed))[seed["main"]] == before[seed["main"]] - AMOUNT


@pytest.mark.asyncio
async def test_an_unmoved_row_settles_on_its_date_as_before(db, seed):
    await _expense(db, seed)
    before = await _balances(db, seed)

    assert await _sweep(db, seed, DUE - datetime.timedelta(days=1)) == 0
    assert await _balances(db, seed) == before
    assert await _sweep(db, seed, DUE) == 1

    [after] = await _rows(db, seed["org_id"])
    assert (after.status, after.settled_date) == (TransactionStatus.SETTLED, DUE)
    assert (await _balances(db, seed))[seed["main"]] == before[seed["main"]] - AMOUNT


@pytest.mark.asyncio
async def test_a_moved_transfer_settles_both_legs_on_the_expected_date(db, seed):
    exp, _inc = await _generated(db, seed)
    await transaction_service.update_transaction(
        db, seed["org_id"], exp.id, TransactionUpdate(settled_date=MOVED),
    )
    before = await _balances(db, seed)

    assert await _sweep(db, seed, DUE) == 0
    assert await _balances(db, seed) == before
    assert await _sweep(db, seed, MOVED) == 1

    rows = await _rows(db, seed["org_id"])
    assert {(t.status, t.settled_date) for t in rows} == {(TransactionStatus.SETTLED, MOVED)}
    after = await _balances(db, seed)
    assert after[seed["main"]] == before[seed["main"]] - AMOUNT
    assert after[seed["savings"]] == before[seed["savings"]] + AMOUNT


@pytest.mark.asyncio
async def test_a_pair_with_one_leg_moved_further_waits_for_both(db, seed):
    """Legs that diverged (an edit before both legs moved together) settle
    together, once the later one is due: never one leg alone."""
    exp, inc = await _generated(db, seed)
    for leg, day in ((exp, MOVED), (inc, MOVED + datetime.timedelta(days=2))):
        leg.settled_date = day
    await db.commit()
    before = await _balances(db, seed)

    assert await _sweep(db, seed, MOVED) == 0
    assert await _balances(db, seed) == before
    assert await _sweep(db, seed, MOVED + datetime.timedelta(days=2)) == 1

    rows = await _rows(db, seed["org_id"])
    assert [t.status for t in rows] == [TransactionStatus.SETTLED] * 2
    after = await _balances(db, seed)
    assert after[seed["main"]] - before[seed["main"]] == -AMOUNT
    assert after[seed["savings"]] - before[seed["savings"]] == AMOUNT
    assert sum(after.values()) - sum(before.values()) == Decimal("0")
