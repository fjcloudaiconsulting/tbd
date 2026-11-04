"""TBD-791: move a pending row's expected settlement date, one row or a batch.

- batch edit sets ``settled_date`` on rows that stay pending (before this they
  were left alone with a note); the row stays pending, ``accounts.balance``
  does not move, and the period filter buckets the row on the new date;
- the date may not be before the transaction date (the row is skipped);
- a pending transfer moves both legs together while both are pending, in the
  single edit and the batch; a settled partner keeps the old rules.

Runs on SQLite in-memory; no MySQL.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.models.transaction import TransactionStatus
from app.schemas.transaction import TransactionUpdate, TransferCreate
from app.services import transaction_service
from app.services.exceptions import ValidationError

from tests.services.test_bulk_update_dates import (  # noqa: F401  (fixture)
    _balance, _bulk, _get, _pending, _recurring_row, _seed, db_session,
)


async def _pending_transfer(db, seed) -> tuple[int, int]:
    exp, inc = await transaction_service.create_transfer(
        db, seed["org_id"],
        TransferCreate(
            from_account_id=seed["src_id"], to_account_id=seed["dst_id"],
            category_id=seed["transfer_cat_id"], amount=Decimal("50"),
            date=date(2026, 5, 1), status="pending",
        ),
    )
    return exp.id, inc.id


# -- batch -----------------------------------------------------------------


async def test_batch_moves_the_expected_settlement_of_pending_rows(db_session):
    seed = await _seed(db_session)
    with_expected = await _pending(db_session, seed, settled=date(2026, 5, 3))
    without = await _pending(db_session, seed)
    occurrence = await _recurring_row(db_session, seed)
    before = await _balance(db_session, seed["src_id"])

    result = await _bulk(
        db_session, seed, [with_expected, without, occurrence],
        settled_date=date(2026, 6, 4),
    )

    assert result == (3, [], [])
    for tx in (with_expected, without, occurrence):
        row = await _get(db_session, tx)
        assert row.status == TransactionStatus.PENDING
        assert row.settled_date == date(2026, 6, 4)
        assert row.date == date(2026, 5, 1)
    assert await _balance(db_session, seed["src_id"]) == before


async def test_period_filter_buckets_the_row_on_the_new_expected_date(db_session):
    seed = await _seed(db_session)
    tx = await _pending(db_session, seed, settled=date(2026, 5, 3))

    async def _in(start, end):
        rows, _ = await transaction_service.list_transactions(
            db_session, seed["org_id"], date_from=start, date_to=end,
        )
        return tx in {r.id for r in rows}

    assert await _in(date(2026, 5, 1), date(2026, 5, 31))
    await _bulk(db_session, seed, [tx], settled_date=date(2026, 6, 4))
    assert not await _in(date(2026, 5, 1), date(2026, 5, 31))
    assert await _in(date(2026, 6, 1), date(2026, 6, 30))


async def test_batch_with_both_dates_sets_both_on_a_pending_row(db_session):
    """The chosen settlement wins over the pending row's kept gap."""
    seed = await _seed(db_session)
    tx = await _pending(db_session, seed, settled=date(2026, 5, 3))

    result = await _bulk(
        db_session, seed, [tx], date=date(2026, 5, 10), settled_date=date(2026, 5, 20),
    )

    assert result == (1, [], [])
    row = await _get(db_session, tx)
    assert (row.status, row.date, row.settled_date) == (
        TransactionStatus.PENDING, date(2026, 5, 10), date(2026, 5, 20),
    )


async def test_batch_drops_an_expected_date_before_the_transaction_date(db_session):
    """Only the settlement date is dropped, with a note; the row's other
    fields still land, and a row with nothing else is skipped with it."""
    seed = await _seed(db_session)
    early = await _pending(db_session, seed, tx_date=date(2026, 5, 1), settled=date(2026, 5, 3))
    late = await _pending(db_session, seed, tx_date=date(2026, 5, 10), settled=date(2026, 5, 12))
    note = "settlement date not applied: before the unchanged date 2026-05-10"

    assert await _bulk(db_session, seed, [early, late], settled_date=date(2026, 5, 5)) == (
        1, [(late, note)], [],
    )
    assert (await _get(db_session, early)).settled_date == date(2026, 5, 5)
    assert (await _get(db_session, late)).settled_date == date(2026, 5, 12)

    assert await _bulk(
        db_session, seed, [late], settled_date=date(2026, 5, 5), category_id=seed["dining_id"],
    ) == (1, [], [(late, note)])
    row = await _get(db_session, late)
    assert (row.category_id, row.settled_date) == (seed["dining_id"], date(2026, 5, 12))


async def test_batch_moves_both_legs_of_a_pending_transfer(db_session):
    seed = await _seed(db_session)
    exp_id, inc_id = await _pending_transfer(db_session, seed)
    before = (await _balance(db_session, seed["src_id"]), await _balance(db_session, seed["dst_id"]))

    result = await _bulk(db_session, seed, [exp_id], settled_date=date(2026, 5, 6))

    assert result == (1, [], [])
    for leg in (exp_id, inc_id):
        row = await _get(db_session, leg)
        assert (row.status, row.settled_date) == (TransactionStatus.PENDING, date(2026, 5, 6))
    after = (await _balance(db_session, seed["src_id"]), await _balance(db_session, seed["dst_id"]))
    assert after == before


async def test_batch_with_both_legs_counts_the_transfer_once(db_session):
    seed = await _seed(db_session)
    exp_id, inc_id = await _pending_transfer(db_session, seed)

    result = await _bulk(db_session, seed, [exp_id, inc_id], settled_date=date(2026, 5, 6))

    assert result == (1, [(
        inc_id, "settlement date already moved with the other leg of this transfer",
    )], [])
    for leg in (exp_id, inc_id):
        assert (await _get(db_session, leg)).settled_date == date(2026, 5, 6)


async def test_batch_drops_a_transfer_date_before_the_other_legs_date(db_session):
    seed = await _seed(db_session)
    exp_id, inc_id = await _pending_transfer(db_session, seed)
    inc = await _get(db_session, inc_id)
    inc.date = date(2026, 5, 4)
    await db_session.commit()

    result = await _bulk(
        db_session, seed, [exp_id], settled_date=date(2026, 5, 2), category_id=seed["internal_id"],
    )

    assert result == (1, [], [(
        exp_id, "settlement date not applied: before the other leg's date 2026-05-04",
    )])
    for leg in (exp_id, inc_id):
        row = await _get(db_session, leg)
        assert (row.category_id, row.settled_date) == (seed["internal_id"], None)


async def test_batch_leaves_a_transfer_with_a_settled_leg_alone(db_session):
    seed = await _seed(db_session)
    exp_id, inc_id = await _pending_transfer(db_session, seed)
    inc = await _get(db_session, inc_id)
    inc.status, inc.settled_date = TransactionStatus.SETTLED, date(2026, 5, 2)
    await db_session.commit()

    result = await _bulk(db_session, seed, [exp_id], settled_date=date(2026, 5, 6))

    assert result == (0, [(
        exp_id, "settlement date not applied: the other leg of this transfer is settled",
    )], [])
    assert (await _get(db_session, exp_id)).settled_date is None
    assert (await _get(db_session, inc_id)).settled_date == date(2026, 5, 2)


# -- single edit -----------------------------------------------------------


async def test_single_edit_moves_both_legs_of_a_pending_transfer(db_session):
    seed = await _seed(db_session)
    exp_id, inc_id = await _pending_transfer(db_session, seed)
    before = (await _balance(db_session, seed["src_id"]), await _balance(db_session, seed["dst_id"]))

    await transaction_service.update_transaction(
        db_session, seed["org_id"], inc_id, TransactionUpdate(settled_date=date(2026, 5, 8)),
    )

    for leg in (exp_id, inc_id):
        row = await _get(db_session, leg)
        assert (row.status, row.settled_date) == (TransactionStatus.PENDING, date(2026, 5, 8))
    after = (await _balance(db_session, seed["src_id"]), await _balance(db_session, seed["dst_id"]))
    assert after == before


async def test_single_edit_resending_the_same_date_leaves_the_partner_alone(db_session):
    """The edit form re-sends the field on every save; a description edit must
    not overwrite a partner leg that already differs."""
    seed = await _seed(db_session)
    exp_id, inc_id = await _pending_transfer(db_session, seed)
    inc = await _get(db_session, inc_id)
    inc.settled_date = date(2026, 5, 9)
    await db_session.commit()

    await transaction_service.update_transaction(
        db_session, seed["org_id"], exp_id,
        TransactionUpdate(description="Savings", settled_date=None),
    )

    assert (await _get(db_session, inc_id)).settled_date == date(2026, 5, 9)


async def test_single_edit_refuses_a_date_before_the_other_legs_date(db_session):
    seed = await _seed(db_session)
    exp_id, inc_id = await _pending_transfer(db_session, seed)
    inc = await _get(db_session, inc_id)
    inc.date = date(2026, 5, 4)
    await db_session.commit()

    with pytest.raises(ValidationError, match="other leg's date"):
        await transaction_service.update_transaction(
            db_session, seed["org_id"], exp_id, TransactionUpdate(settled_date=date(2026, 5, 2)),
        )
    await db_session.rollback()
    assert (await _get(db_session, exp_id)).settled_date is None


async def test_single_edit_with_a_settled_partner_moves_only_this_leg(db_session):
    seed = await _seed(db_session)
    exp_id, inc_id = await _pending_transfer(db_session, seed)
    inc = await _get(db_session, inc_id)
    inc.status, inc.settled_date = TransactionStatus.SETTLED, date(2026, 5, 2)
    await db_session.commit()

    await transaction_service.update_transaction(
        db_session, seed["org_id"], exp_id, TransactionUpdate(settled_date=date(2026, 5, 7)),
    )

    assert (await _get(db_session, exp_id)).settled_date == date(2026, 5, 7)
    assert (await _get(db_session, inc_id)).settled_date == date(2026, 5, 2)


async def test_single_edit_of_a_pending_row_keeps_the_balance(db_session):
    seed = await _seed(db_session)
    tx = await _pending(db_session, seed, settled=date(2026, 5, 3))
    before = await _balance(db_session, seed["src_id"])

    await transaction_service.update_transaction(
        db_session, seed["org_id"], tx, TransactionUpdate(settled_date=date(2026, 5, 9)),
    )

    row = await _get(db_session, tx)
    assert (row.status, row.settled_date) == (TransactionStatus.PENDING, date(2026, 5, 9))
    assert await _balance(db_session, seed["src_id"]) == before
