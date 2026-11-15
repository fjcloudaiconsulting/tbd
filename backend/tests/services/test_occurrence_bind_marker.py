"""The durable bind marker and the guards that read it (TBD-755 PR2, TBD-807).

A bank row that settles a recurring occurrence carries the occurrence's
pre-bind state (``bind_prev_*``); the occurrence points back at it
(``bound_bank_tx_id``). While bound, neither row can be paired, deleted, or have
its money edited, so the payment can never be counted or projected twice.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import ImportBatch
from app.models.transaction import TransactionStatus, TransactionType
from app.schemas.transaction import TransactionUpdate
from app.services import reconciliation_service, recurring_service, transaction_service
from app.services.exceptions import ConflictError, ValidationError
from tests.services.test_reconcile_match_recurring_occurrence import (
    B,
    JUN_1,
    JUN_2,
    MAY_29,
    OPENING_A,
    T,
    _account,
    _bank,
    _create,
    _match,
    _occurrence,
    _reload,
    _seed,
    _snapshot,
    _template,
    assert_invariant,
    db_session,  # noqa: F401  (fixture)
)


async def _bound(db, *, auto_settle=False, paid=MAY_29):
    seed = await _seed(db)
    r = await _template(db, seed, auto_settle=auto_settle)
    o = await _occurrence(db, seed, r)
    bank = await _bank(db, seed, paid=paid)
    await _match(db, seed, bank.id, o.id)
    return seed, await _reload(db, o.id), await _reload(db, bank.id)


@pytest.mark.parametrize("auto_settle", [False, True], ids=["pending", "auto_settled"])
async def test_match_writes_the_marker(db_session, auto_settle):
    """Kills: a snapshot taken after the settle (prev == bank values), a missing
    back-pointer, and dropping ``settled_date`` / ``status`` from the snapshot."""
    paid = JUN_2 if auto_settle else MAY_29
    seed, o, bank = await _bound(db_session, auto_settle=auto_settle, paid=paid)

    assert o.bound_bank_tx_id == bank.id
    assert bank.bind_prev_amount == Decimal(T)
    assert bank.bind_prev_date == JUN_1
    if auto_settle:
        assert (bank.bind_prev_status, bank.bind_prev_settled_date) == (
            TransactionStatus.SETTLED, JUN_1,
        )
    else:
        assert bank.bind_prev_status == TransactionStatus.PENDING
    assert (o.amount, o.status) == (Decimal(B), TransactionStatus.SETTLED)
    await assert_invariant(db_session, seed)


@pytest.mark.parametrize("side", ["occurrence", "bank_row"])
async def test_a_bound_row_cannot_be_paired(db_session, side):
    """TBD-807a. Kills: the ``_link_pair`` guard missing either marker."""
    seed, o, bank = await _bound(db_session)
    income = await _create(
        db_session, seed, account_id=seed["acct_b_id"], amount=B, label="income",
        tx_type=TransactionType.INCOME, tx_date=JUN_1,
    )
    leg_id = o.id if side == "occurrence" else bank.id
    before = await _snapshot(db_session, seed)
    with pytest.raises(ConflictError) as exc:
        await transaction_service.pair_existing_transactions(
            db_session, seed["org_id"], leg_id, income.id, recategorize=False,
        )
    assert exc.value.code == "transaction_bound"
    await db_session.rollback()
    db_session.expunge_all()
    assert await _snapshot(db_session, seed) == before


@pytest.mark.parametrize("edit", [
    {"status": "pending"}, {"date": JUN_1}, {"settled_date": JUN_2},
    {"amount": Decimal("1.00")},
])
async def test_a_bound_occurrence_keeps_its_money(db_session, edit):
    """TBD-807b: flipping it back to pending (a second projection) or moving its
    money. Kills dropping any field from the guard."""
    seed, o, _ = await _bound(db_session)
    with pytest.raises(ConflictError) as exc:
        await transaction_service.update_transaction(
            db_session, seed["org_id"], o.id, TransactionUpdate(**edit),
        )
    assert exc.value.code == "transaction_bound"


async def test_an_unchanged_money_field_is_not_an_edit(db_session):
    """The guard compares values: a PUT echoing the row back (the edit form
    sends every field) must still land a description change."""
    seed, o, _ = await _bound(db_session)
    out = await transaction_service.update_transaction(
        db_session, seed["org_id"], o.id,
        TransactionUpdate(description="Energy (paid)", amount=o.amount, status="settled",
                          date=o.date, settled_date=o.settled_date),
    )
    assert out.description == "Energy (paid)"
    await assert_invariant(db_session, seed)


@pytest.mark.parametrize("side", ["occurrence", "bank_row"])
@pytest.mark.parametrize("bulk", [False, True], ids=["single", "bulk"])
async def test_a_bound_row_cannot_be_deleted(db_session, side, bulk):
    """Kills: TBD-294's silent REJECT dropping the payment; a bulk path that
    skips the guard."""
    seed, o, bank = await _bound(db_session)
    target = o.id if side == "occurrence" else bank.id
    with pytest.raises(ConflictError) as exc:
        if bulk:
            await transaction_service.bulk_delete_transactions(db_session, seed["org_id"], [target])
        else:
            await transaction_service.delete_transaction(db_session, seed["org_id"], target)
    assert exc.value.code == "transaction_bound"
    await db_session.rollback()
    assert (await _account(db_session, seed["acct_a_id"])).balance == OPENING_A - Decimal(B)


async def test_unique_back_pointer(db_session):
    """The database backstop: two occurrences cannot name one bank row."""
    seed, o, bank = await _bound(db_session)
    other = await _create(db_session, seed, account_id=seed["acct_a_id"], amount=T, label="x",
                          tx_date=JUN_1)
    other.bound_bank_tx_id = bank.id
    with pytest.raises(IntegrityError):
        await db_session.flush()


# ── unbind ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("auto_settle", [False, True], ids=["pending", "auto_settled"])
async def test_unbind_restores_both_rows(db_session, auto_settle):
    """Kills: restoring from the bank values instead of the snapshot; skipping
    the occurrence's re-apply when it was auto-settled (balance off by T);
    leaving the bank row out of the balance; leaving it MATCHED with no link."""
    paid = JUN_2 if auto_settle else MAY_29
    seed, o, bank = await _bound(db_session, auto_settle=auto_settle, paid=paid)
    batch = await db_session.get(ImportBatch, bank.import_batch_id)
    accepted_before = batch.accepted_count

    await reconciliation_service.unbind_occurrence(
        db_session, org_id=seed["org_id"], bank_tx_id=bank.id,
    )

    o, bank = await _reload(db_session, o.id), await _reload(db_session, bank.id)
    assert (o.amount, o.date, o.bound_bank_tx_id) == (Decimal(T), JUN_1, None)
    if auto_settle:
        assert (o.status, o.settled_date) == (TransactionStatus.SETTLED, JUN_1)
        expected = OPENING_A - Decimal(T) - Decimal(B)
    else:
        assert (o.status, o.settled_date) == (TransactionStatus.PENDING, None)
        expected = OPENING_A - Decimal(B)
    assert bank.linked_transaction_id is None and bank.bind_prev_status is None
    assert bank.bind_prev_amount is None and bank.bind_prev_date is None
    assert bank.reconciliation_state == "accepted"
    await db_session.refresh(batch)
    assert batch.accepted_count == accepted_before + 1
    assert (await _account(db_session, seed["acct_a_id"])).balance == expected
    await assert_invariant(db_session, seed)


async def test_an_unbound_occurrence_can_be_bound_again(db_session):
    """The way out of a wrong bind: unbind, then match the right bank row."""
    seed, o, bank = await _bound(db_session)
    await reconciliation_service.unbind_occurrence(
        db_session, org_id=seed["org_id"], bank_tx_id=bank.id,
    )
    b2 = await _bank(db_session, seed, amount="370.00", paid=JUN_2)
    await _match(db_session, seed, b2.id, o.id)
    o = await _reload(db_session, o.id)
    assert (o.amount, o.bound_bank_tx_id) == (Decimal("370.00"), b2.id)
    assert (await _account(db_session, seed["acct_a_id"])).balance == (
        OPENING_A - Decimal(B) - Decimal("370.00")
    )
    await assert_invariant(db_session, seed)


async def test_unbind_refusals(db_session):
    """Kills: unbinding a row with no snapshot, or after the series stopped."""
    seed, o, bank = await _bound(db_session)
    plain = await _create(db_session, seed, account_id=seed["acct_a_id"], amount=T, label="p",
                          tx_date=JUN_1)
    with pytest.raises(ValidationError) as exc:
        await reconciliation_service.unbind_occurrence(
            db_session, org_id=seed["org_id"], bank_tx_id=plain.id,
        )
    assert exc.value.code == "not_bound"

    await recurring_service.stop_recurring(db_session, seed["org_id"], o.recurring_id)
    before = await _snapshot(db_session, seed)
    with pytest.raises(ConflictError) as exc:
        await reconciliation_service.unbind_occurrence(
            db_session, org_id=seed["org_id"], bank_tx_id=bank.id,
        )
    assert exc.value.code == "series_stopped"
    await db_session.rollback()
    db_session.expunge_all()
    assert await _snapshot(db_session, seed) == before


async def test_unbind_route_returns_both_rows(db_session):
    from types import SimpleNamespace

    from app.routers.transactions import unbind_transaction

    seed, o, bank = await _bound(db_session)
    out = await unbind_transaction(
        bank.id, current_user=SimpleNamespace(org_id=seed["org_id"]), db=db_session,
    )
    assert [r.id for r in out] == sorted([o.id, bank.id])
    assert {r.id: r.status for r in out}[o.id] == "pending"


async def test_inbox_row_shows_the_bound_occurrence(db_session):
    """Kills: no ``bound_occurrence`` on the row (nothing to unbind from in the
    UI), and ``can_unbind`` ignoring a stopped series."""
    seed, o, bank = await _bound(db_session)
    detail = await reconciliation_service.get_batch_detail(
        db_session, org_id=seed["org_id"], batch_id=bank.import_batch_id,
    )
    row = next(r for r in detail.rows if r.transaction_id == bank.id)
    assert row.bound_occurrence.occurrence_id == o.id
    assert row.bound_occurrence.can_unbind is True

    await recurring_service.stop_recurring(db_session, seed["org_id"], o.recurring_id)
    detail = await reconciliation_service.get_batch_detail(
        db_session, org_id=seed["org_id"], batch_id=bank.import_batch_id,
    )
    row = next(r for r in detail.rows if r.transaction_id == bank.id)
    assert row.bound_occurrence.can_unbind is False
