"""Import binds a bank row to the recurring occurrence it settles (TBD-755 PR2).

The headline case: a charge due Monday 1 June that the bank debits on Friday
29 May appears ONCE, settled, against its own occurrence, and generation never
writes a second one. Detector 3 proposes the bind in the preview; the confirm's
``bind_occurrence`` action applies it, creating the occurrence when generation
has not written it yet.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.models.transaction import Transaction, TransactionStatus
from app.schemas.import_schemas import ImportConfirmRequest, ImportConfirmRow
from app.services import import_service, reconciliation_service, recurring_service
from app.services.import_parser import ParsedRow
from tests.services.test_reconcile_match_recurring_occurrence import (
    B,
    JUL_1,
    JUN_1,
    MAY_29,
    OPENING_A,
    T,
    _account,
    _occurrence,
    _reload,
    _seed,
    _series_row,
    _template,
    assert_invariant,
    db_session,  # noqa: F401  (fixture)
)

JUN_29 = date(2026, 6, 29)
JUN_30 = date(2026, 6, 30)


def _parsed(desc="Energy", amount=B, on=MAY_29, n=1) -> ParsedRow:
    return ParsedRow(row_number=n, date=on, description=desc, amount=Decimal(amount), type="expense")


async def _preview(db, seed, rows, today=JUN_1):
    return await import_service.build_preview(
        db, seed["org_id"], seed["acct_a_id"], "bank.csv", rows, today=today,
    )


async def _confirm(db, seed, preview_row, *, today=JUN_1, occurrence_id="from_preview"):
    c = preview_row.occurrence_candidates[0]
    body = ImportConfirmRequest(
        account_id=seed["acct_a_id"], default_category_id=seed["cat_id"],
        file_name="bank.csv", source_format="csv",
        rows=[ImportConfirmRow(
            row_number=preview_row.row_number, date=preview_row.date,
            description=preview_row.description, amount=preview_row.amount,
            type=preview_row.type, action="bind_occurrence",
            bind_recurring_id=c.recurring_id, bind_due_date=c.due_date,
            bind_occurrence_id=(
                c.occurrence_id if occurrence_id == "from_preview" else occurrence_id
            ),
        )],
    )
    return await import_service.execute_import(db, seed["org_id"], body, today=today)


async def _series_count(db, recurring_id):
    return await db.scalar(
        select(func.count(Transaction.id)).where(Transaction.recurring_id == recurring_id)
    )


# ── detector 3 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("desc,amount,action,signal", [
    ("Energy", B, "bind", "merchant"),          # same merchant, new amount: binds
    ("ESSENT 8812", T, "suggest_bind", "amount"),  # amount only: a suggestion
    ("ESSENT 8812", B, "none", None),          # amount AND merchant differ
])
async def test_preview_classifies_a_pulled_forward_charge(db_session, desc, amount, action, signal):
    """Kills: amount alone preselecting; a no-match on a same-merchant price
    change; matching when neither amount nor merchant agrees."""
    seed = await _seed(db_session)
    r = await _template(db_session, seed)
    o = await _occurrence(db_session, seed, r)

    out = await _preview(db_session, seed, [_parsed(desc, amount)])

    row = out.rows[0]
    assert row.occurrence_match_action == action
    assert out.bind_count == (1 if action == "bind" else 0)
    if signal:
        (c,) = row.occurrence_candidates
        assert (c.occurrence_id, c.recurring_id, c.due_date, c.signal) == (o.id, r.id, JUN_1, signal)
        assert c.date_diff_days == 3
    else:
        assert row.occurrence_candidates == []


async def test_preview_offers_an_occurrence_not_written_yet(db_session):
    """The ticket's own premise: generation wrote only up to the cycle end, so
    the July occurrence does not exist when the 29 June debit is imported."""
    seed = await _seed(db_session)
    r = await _template(db_session, seed, next_due=JUL_1)

    out = await _preview(db_session, seed, [_parsed(on=JUN_29)], today=JUN_29)

    (c,) = out.rows[0].occurrence_candidates
    assert out.rows[0].occurrence_match_action == "bind"
    assert (c.occurrence_id, c.recurring_id, c.due_date) == (None, r.id, JUL_1)


async def test_two_rows_never_share_a_preselected_occurrence(db_session):
    """Kills: the claim pass; both rows would bind one occurrence."""
    seed = await _seed(db_session)
    r = await _template(db_session, seed)
    await _occurrence(db_session, seed, r)

    out = await _preview(db_session, seed, [_parsed(n=1), _parsed(on=JUN_1, n=2)])

    assert [x.occurrence_match_action for x in out.rows] == ["choose_occurrence"] * 2


async def test_window_is_four_days(db_session):
    seed = await _seed(db_session)
    r = await _template(db_session, seed)
    await _occurrence(db_session, seed, r)
    out = await _preview(db_session, seed, [_parsed(on=date(2026, 5, 27))])
    assert out.rows[0].occurrence_match_action == "none"


# ── confirm ──────────────────────────────────────────────────────────────────


async def test_confirm_binds_a_pending_occurrence_once(db_session):
    """The acceptance case. Kills: the bank row left in the balance (two
    charges), the occurrence left pending (a ghost), and a second occurrence
    from the next generation run."""
    seed = await _seed(db_session)
    r = await _template(db_session, seed)
    o = await _occurrence(db_session, seed, r)
    out = await _preview(db_session, seed, [_parsed()])

    res = await _confirm(db_session, seed, out.rows[0])

    assert (res.bound_count, res.imported_count, res.bind_fallbacks) == (1, 0, [])
    o = await _reload(db_session, o.id)
    assert (o.status, o.amount, o.date, o.settled_date) == (
        TransactionStatus.SETTLED, Decimal(B), MAY_29, MAY_29,
    )
    bank = await db_session.get(Transaction, o.bound_bank_tx_id)
    assert bank.linked_transaction_id == o.id and bank.import_batch_id is None
    assert (await _account(db_session, seed["acct_a_id"])).balance == OPENING_A - Decimal(B)
    await recurring_service.generate_due_transactions(db_session, seed["org_id"], today=JUN_30)
    assert await _series_count(db_session, r.id) == 1
    await assert_invariant(db_session, seed)


async def test_confirm_creates_the_occurrence_it_binds(db_session):
    """Create path: the occurrence is written, settled and spent in the
    confirm, so generation never writes 1 July again."""
    seed = await _seed(db_session)
    r = await _template(db_session, seed, next_due=JUL_1)
    out = await _preview(db_session, seed, [_parsed(on=JUN_29)], today=JUN_29)

    res = await _confirm(db_session, seed, out.rows[0], today=JUN_29)

    assert res.bound_count == 1
    occ = await _series_row(db_session, r.id, JUN_29)
    assert (occ.status, occ.amount, occ.bound_bank_tx_id is not None) == (
        TransactionStatus.SETTLED, Decimal(B), True,
    )
    await db_session.refresh(r)
    assert r.next_due_date == date(2026, 8, 1)
    await recurring_service.generate_due_transactions(db_session, seed["org_id"], today=date(2026, 7, 5))
    assert await _series_count(db_session, r.id) == 1
    assert (await _account(db_session, seed["acct_a_id"])).balance == OPENING_A - Decimal(B)
    await assert_invariant(db_session, seed)

    # Unbind keeps the created occurrence, pending at its due date.
    await reconciliation_service.unbind_occurrence(
        db_session, org_id=seed["org_id"], bank_tx_id=occ.bound_bank_tx_id,
    )
    occ = await _reload(db_session, occ.id)
    assert (occ.status, occ.amount, occ.date) == (TransactionStatus.PENDING, Decimal(T), JUL_1)
    assert (await _account(db_session, seed["acct_a_id"])).balance == OPENING_A - Decimal(B)
    await assert_invariant(db_session, seed)


async def test_a_refused_bind_still_imports_the_row(db_session):
    """Kills: a refused bind dropping the bank row (sync must never lose one).
    Here generation wrote the frontier after the preview offered to create it."""
    seed = await _seed(db_session)
    r = await _template(db_session, seed, next_due=JUL_1)
    out = await _preview(db_session, seed, [_parsed(on=JUN_29)], today=JUN_29)
    await recurring_service.generate_due_transactions(db_session, seed["org_id"], today=JUL_1)

    res = await _confirm(db_session, seed, out.rows[0], today=JUL_1)

    assert (res.bound_count, res.imported_count, len(res.bind_fallbacks)) == (0, 1, 1)
    assert (await _account(db_session, seed["acct_a_id"])).balance == OPENING_A - Decimal(B)
    occ = await _series_row(db_session, r.id, JUL_1)
    assert occ.status == TransactionStatus.PENDING and occ.bound_bank_tx_id is None
    await assert_invariant(db_session, seed)


async def test_a_database_error_is_not_a_row_error(db_session):
    """Kills: catching OperationalError per row. On MySQL a deadlock has
    already rolled back the whole transaction."""
    seed = await _seed(db_session)
    r = await _template(db_session, seed)
    await _occurrence(db_session, seed, r)
    out = await _preview(db_session, seed, [_parsed()])
    boom = OperationalError("UPDATE", {}, Exception("1213 Deadlock found"))
    with patch.object(reconciliation_service, "bind_bank_row", AsyncMock(side_effect=boom)):
        with pytest.raises(OperationalError):
            await _confirm(db_session, seed, out.rows[0])
