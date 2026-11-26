"""A reconcile MATCH onto a recurring occurrence settles it (TBD-755 PR1).

Before TBD-755 a bank row matched onto the PENDING occurrence of a series was
reverted out of ``accounts.balance`` while the occurrence stayed PENDING, so
the bank amount was counted nowhere. ``_apply_match`` now routes a match onto a
*bindable* occurrence through ``_settle_occurrence_from_bank``, which settles
it at the bank amount and payment date, and refuses the matches that would
drop or double-count money.

Fixture rules are inherited from ``test_matched_row_actions``: every match is
built through the real ``reconcile_request``, every balance is asserted per
account against the SQL reconstruction, and every service that reads a clock is
handed ``today=``.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import Account, ImportBatch, Organization, Transaction
from app.models.recurring import RecurringTransaction
from app.models.transaction import TransactionStatus, TransactionType
from app.schemas.import_reconciliation import (
    ReconcileBatchRequest,
    ReconciliationEdits,
    ReconciliationState,
    ReconciliationTransition,
)
from app.schemas.recurring import RecurringCreate
from app.schemas.transaction import PromoteToRecurringRequest, TransactionUpdate
from app.services import currency_service, recurring_service, transaction_service
from app.services.exceptions import ConflictError, ValidationError
from app.services.spending_service import executed_expense_by_category
from app.services.transaction_filters import (
    effective_period_date_expr,
    is_reciprocal_pair,
    reportable_transaction_filter,
)
from tests.services.test_matched_row_actions import (  # noqa: F401  (db_session fixture)
    _account,
    _create,
    _legacy_pre_tbd755_match,
    _reconcile,
    _reload,
    _seed,
    _transition,
    assert_invariant,
    db_session,
)

JUN_1 = date(2026, 6, 1)  # Monday, the start of period B
JUN_2 = date(2026, 6, 2)
JUL_1 = date(2026, 7, 1)
MAY_1 = date(2026, 5, 1)
MAY_29 = date(2026, 5, 29)
MAY_30 = date(2026, 5, 30)
MAY_31 = date(2026, 5, 31)
JUN_30 = date(2026, 6, 30)
T = "107.00"  # the occurrence's own amount
B = "365.00"  # the bank amount (V1: never equal to T)
OPENING_A = Decimal("1000.00")


# ── helpers ──────────────────────────────────────────────────────────────────


async def _template(
    db, seed, *, amount=T, auto_settle=False, type="expense",
    account="acct_a_id", to_account=None, next_due=JUN_1, description="Energy",
) -> RecurringTransaction:
    return await recurring_service.create_recurring(
        db, seed["org_id"],
        RecurringCreate(
            account_id=seed[account], category_id=seed["cat_id"],
            description=description, amount=Decimal(amount), type=type,
            to_account_id=seed[to_account] if to_account else None,
            frequency="monthly", next_due_date=next_due, auto_settle=auto_settle,
        ),
        today=JUN_1,
    )


async def _occurrence(db, seed, r: RecurringTransaction, due: date = JUN_1) -> Transaction:
    """Run generation and return ``r``'s row at ``due`` (a transfer's expense leg)."""
    await recurring_service.generate_due_transactions(db, seed["org_id"], today=JUN_1)
    return await _series_row(db, r.id, due)


async def _series_row(db, recurring_id: int, due: date) -> Transaction:
    return await db.scalar(
        select(Transaction)
        .where(Transaction.recurring_id == recurring_id, Transaction.date == due)
        .order_by(Transaction.id)
        .limit(1)
        .execution_options(populate_existing=True)
    )


async def _series_count(db, recurring_id: int) -> int:
    return await db.scalar(
        select(func.count(Transaction.id)).where(Transaction.recurring_id == recurring_id)
    )


async def _bank(db, seed, *, amount=B, paid=MAY_29, account="acct_a_id",
                tx_type=TransactionType.EXPENSE,
                status=TransactionStatus.SETTLED) -> Transaction:
    return await _create(
        db, seed, account_id=seed[account], amount=amount, label="bank",
        tx_type=tx_type, status=status, in_batch=True, tx_date=paid,
    )


async def _match(db, seed, bank_id: int, target_id: int):
    return await _reconcile(
        db, seed, _transition(bank_id, ReconciliationState.MATCHED, match=target_id)
    )


async def _reopen(db, seed, bank_id: int):
    await _reconcile(db, seed, _transition(bank_id, ReconciliationState.ACCEPTED))
    await _reconcile(db, seed, _transition(bank_id, ReconciliationState.PENDING_REVIEW))


async def _period_reportable(db, org_id, account_id, start, end) -> Decimal:
    raw = await db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.org_id == org_id,
            Transaction.account_id == account_id,
            reportable_transaction_filter(),
            effective_period_date_expr().between(start, end),
        )
    )
    return Decimal(str(raw)).quantize(Decimal("0.01"))


async def _frontier(db, r: RecurringTransaction):
    r = await db.scalar(
        select(RecurringTransaction)
        .where(RecurringTransaction.id == r.id)
        .execution_options(populate_existing=True)
    )
    return (r.next_due_date, r.occurrences_elapsed, r.last_spent_date)


async def _snapshot(db, seed) -> dict:
    """Every account balance and every row of the org: "nothing was written"."""
    db.expunge_all()
    accts = (await db.scalars(
        select(Account).where(Account.org_id == seed["org_id"]).order_by(Account.id)
    )).all()
    rows = (await db.scalars(
        select(Transaction).where(Transaction.org_id == seed["org_id"]).order_by(Transaction.id)
    )).all()
    return {
        "accounts": {a.id: a.balance for a in accts},
        "rows": {
            t.id: (t.amount, t.date, t.settled_date, t.status, t.linked_transaction_id,
                   t.reconciliation_state)
            for t in rows
        },
    }


# ── #1-#3: dates ─────────────────────────────────────────────────────────────


async def test_pulled_forward_bind_lands_in_the_payment_period(db_session):
    """#1. Kills: ``settled_date = due``; the helper calling ``_advance_frontier``;
    ``last_spent_date = bank date``; re-pricing only when ``was_settled``."""
    seed = await _seed(db_session)
    r = await _template(db_session, seed)
    o = await _occurrence(db_session, seed, r)
    assert o.status == TransactionStatus.PENDING and o.amount == Decimal(T)
    frontier = await _frontier(db_session, r)
    bank = await _bank(db_session, seed, paid=MAY_29)

    await _match(db_session, seed, bank.id, o.id)

    o = await _reload(db_session, o.id)
    assert o.date == o.settled_date == MAY_29
    assert o.amount == Decimal(B)
    assert o.status == TransactionStatus.SETTLED
    org, acct = seed["org_id"], seed["acct_a_id"]
    assert await _period_reportable(db_session, org, acct, MAY_1, MAY_31) == Decimal(B)
    assert await _period_reportable(db_session, org, acct, JUN_1, JUN_30) == Decimal("0.00")
    pending_june = await db_session.scalar(
        select(func.count(Transaction.id)).where(
            Transaction.recurring_id == r.id,
            Transaction.status == TransactionStatus.PENDING,
            Transaction.date.between(JUN_1, JUN_30),
        )
    )
    assert pending_june == 0
    assert await _frontier(db_session, r) == frontier

    before = await _series_count(db_session, r.id)
    await recurring_service.generate_due_transactions(db_session, org, today=JUN_1)
    assert await _series_count(db_session, r.id) == before
    assert await _frontier(db_session, r) == frontier
    await assert_invariant(db_session, seed)


async def test_pushed_back_bind_keeps_the_due_date(db_session):
    """#2. Kills: ``date = paid``, ``date = max(due, paid)``."""
    seed = await _seed(db_session)
    r = await _template(db_session, seed)
    o = await _occurrence(db_session, seed, r)
    bank = await _bank(db_session, seed, paid=JUN_2)

    await _match(db_session, seed, bank.id, o.id)

    o = await _reload(db_session, o.id)
    assert o.date == JUN_1
    assert o.settled_date == JUN_2
    assert o.amount == Decimal(B)
    await assert_invariant(db_session, seed)


async def test_later_edit_of_a_bound_occurrence_succeeds(db_session):
    """#3. Kills: ``date = due`` with ``settled_date = paid`` (every later PUT
    400s on ``settled_date must be on or after date``). PR2: the bound pair's
    money is the bank's, so an amount edit is refused (TBD-807b) while a
    description edit still lands."""
    seed = await _seed(db_session)
    r = await _template(db_session, seed)
    o = await _occurrence(db_session, seed, r)
    bank = await _bank(db_session, seed, paid=MAY_29)
    await _match(db_session, seed, bank.id, o.id)

    out = await transaction_service.update_transaction(
        db_session, seed["org_id"], o.id, TransactionUpdate(description="Energy (May)"),
    )
    assert out.description == "Energy (May)"
    before = (await _account(db_session, seed["acct_a_id"])).balance
    with pytest.raises(ConflictError) as exc:
        await transaction_service.update_transaction(
            db_session, seed["org_id"], o.id, TransactionUpdate(amount=Decimal("400.00")),
        )
    assert exc.value.code == "transaction_bound"
    await db_session.rollback()
    assert (await _account(db_session, seed["acct_a_id"])).balance == before
    await assert_invariant(db_session, seed)


# ── #4-#5: amounts ───────────────────────────────────────────────────────────


async def test_auto_settled_occurrence_is_repriced(db_session):
    """#4. Kills: no revert of the old amount (opening-472); the helper skipped
    when the occurrence is SETTLED (opening-107)."""
    seed = await _seed(db_session)
    r = await _template(db_session, seed, auto_settle=True)
    o = await _occurrence(db_session, seed, r)
    assert o.status == TransactionStatus.SETTLED and o.settled_date == JUN_1
    bank = await _bank(db_session, seed, paid=JUN_2)

    await _match(db_session, seed, bank.id, o.id)

    assert (await _account(db_session, seed["acct_a_id"])).balance == OPENING_A - Decimal(B)
    o = await _reload(db_session, o.id)
    assert o.amount == Decimal(B)
    assert o.settled_date == JUN_2
    await assert_invariant(db_session, seed)


@pytest.mark.parametrize("auto_settle", [False, True], ids=["pending", "auto_settled"])
@pytest.mark.parametrize("tx_type", [TransactionType.EXPENSE, TransactionType.INCOME],
                         ids=["expense", "income"])
async def test_bank_amount_counted_exactly_once(db_session, auto_settle, tx_type):
    """#5. Kills: the helper also reverting ``bank_tx``; a back-link or
    ``_link_pair``; re-pricing only when ``was_settled``."""
    seed = await _seed(db_session)
    r = await _template(db_session, seed, auto_settle=auto_settle, type=tx_type.value)
    o = await _occurrence(db_session, seed, r)
    bank = await _bank(db_session, seed, paid=MAY_29, tx_type=tx_type)

    await _match(db_session, seed, bank.id, o.id)

    sign = Decimal(1) if tx_type == TransactionType.INCOME else Decimal(-1)
    assert (await _account(db_session, seed["acct_a_id"])).balance == OPENING_A + sign * Decimal(B)
    await assert_invariant(db_session, seed)
    total = await _period_reportable(
        db_session, seed["org_id"], seed["acct_a_id"], date(2000, 1, 1), date(2100, 1, 1),
    )
    assert total == Decimal(B)
    o = await _reload(db_session, o.id)
    bank = await _reload(db_session, bank.id)
    assert o.linked_transaction_id is None
    assert bank.linked_transaction_id == o.id
    assert is_reciprocal_pair(bank, o) is False


# ── #6-#9b: links ────────────────────────────────────────────────────────────


async def test_rematch_of_a_legacy_link_repairs_the_balance(db_session):
    """#6. Kills: the referrer probe without ``id != bank_tx.id``."""
    seed = await _seed(db_session)
    r = await _template(db_session, seed)
    o = await _occurrence(db_session, seed, r)
    bank = await _bank(db_session, seed, paid=MAY_29)
    with _legacy_pre_tbd755_match():
        await _match(db_session, seed, bank.id, o.id)
    assert (await _account(db_session, seed["acct_a_id"])).balance == OPENING_A  # B lost
    assert (await _reload(db_session, o.id)).status == TransactionStatus.PENDING

    await _reopen(db_session, seed, bank.id)
    await _match(db_session, seed, bank.id, o.id)

    assert (await _account(db_session, seed["acct_a_id"])).balance == OPENING_A - Decimal(B)
    o = await _reload(db_session, o.id)
    assert (o.status, o.amount) == (TransactionStatus.SETTLED, Decimal(B))
    await assert_invariant(db_session, seed)


async def test_match_onto_a_pending_non_recurring_row_is_refused(db_session):
    """#7 (rulings §5). Kills: no guard (the bank row is reverted while the
    target adds nothing)."""
    seed = await _seed(db_session)
    target = await _create(
        db_session, seed, account_id=seed["acct_a_id"], amount=T, label="hand-keyed",
        status=TransactionStatus.PENDING, tx_date=JUN_1,
    )
    bank = await _bank(db_session, seed)
    before = await _snapshot(db_session, seed)

    with pytest.raises(ValidationError) as exc:
        await _match(db_session, seed, bank.id, target.id)
    assert exc.value.code == "match_target_pending"

    assert await _snapshot(db_session, seed) == before
    bank = await _reload(db_session, bank.id)
    assert bank.linked_transaction_id is None
    assert bank.reconciliation_state == "pending_review"


async def test_imported_row_with_a_recurring_id_takes_the_ordinary_match(db_session):
    """#8. Kills: the bindable predicate keyed on ``recurring_id`` alone (the
    target would be re-priced to 365 and re-dated to 05-29)."""
    seed = await _seed(db_session)
    imported = await _create(
        db_session, seed, account_id=seed["acct_a_id"], amount=T, label="imported",
        tx_date=JUN_1,
    )
    imported.is_imported = True
    await db_session.commit()
    await transaction_service.promote_to_recurring(
        db_session, seed["org_id"], imported.id,
        PromoteToRecurringRequest(frequency="monthly", next_due_date=JUL_1),
        today=JUN_1,
    )
    imported = await _reload(db_session, imported.id)
    assert imported.recurring_id is not None
    bank = await _bank(db_session, seed, paid=MAY_29)

    await _match(db_session, seed, bank.id, imported.id)

    imported = await _reload(db_session, imported.id)
    assert (imported.amount, imported.date, imported.settled_date) == (Decimal(T), JUN_1, JUN_1)
    # The bank row was reverted: only the target is in the balance.
    assert (await _account(db_session, seed["acct_a_id"])).balance == OPENING_A - Decimal(T)
    await assert_invariant(db_session, seed)


@pytest.mark.parametrize("one_request", [False, True], ids=["separate", "one_request"])
async def test_second_bank_row_onto_a_bound_occurrence_is_refused(db_session, one_request):
    """#9. Kills: the helper without the referrer probe (the occurrence is
    re-priced to B2 and B1 vanishes). SEQUENTIAL ONLY: it proves nothing about
    concurrency (spec §1.6)."""
    seed = await _seed(db_session)
    r = await _template(db_session, seed)
    o = await _occurrence(db_session, seed, r)
    b1 = await _bank(db_session, seed, paid=MAY_29)
    b2 = await _bank(db_session, seed, amount="300.00", paid=MAY_30)

    if one_request:
        before = await _snapshot(db_session, seed)
        request = ReconcileBatchRequest(transitions=[
            ReconciliationTransition(transaction_id=b.id, to_state=ReconciliationState.MATCHED,
                                     match_with_transaction_id=o.id)
            for b in (b1, b2)
        ])
        with pytest.raises(ConflictError) as exc:
            await _reconcile(db_session, seed, request)
        assert exc.value.code == "occurrence_already_bound"
        assert await _snapshot(db_session, seed) == before
        return

    o_id, b2_id = o.id, b2.id  # the refusal's rollback expires the instances
    await _match(db_session, seed, b1.id, o_id)
    with pytest.raises(ConflictError) as exc:
        await _match(db_session, seed, b2_id, o_id)
    assert exc.value.code == "occurrence_already_bound"
    db_session.expunge_all()
    o = await _reload(db_session, o_id)
    assert o.amount == Decimal(B)
    assert (await _reload(db_session, b2_id)).linked_transaction_id is None
    assert (await _account(db_session, seed["acct_a_id"])).balance == (
        OPENING_A - Decimal(B) - Decimal("300.00")
    )
    await assert_invariant(db_session, seed)


async def test_skipped_earlier_bank_row_does_not_block_a_rebind(db_session):
    """#9b. Kills: the referrer probe without the reverted-state filter (409,
    and the occurrence is stuck)."""
    seed = await _seed(db_session)
    r = await _template(db_session, seed)
    o = await _occurrence(db_session, seed, r)
    b1 = await _bank(db_session, seed, paid=MAY_29)
    # A legacy pre-TBD-755 match (still in production): link, no settle, no
    # marker, so it can be skipped. A marker-bound row cannot (PR2).
    with _legacy_pre_tbd755_match():
        await _match(db_session, seed, b1.id, o.id)
    await _reopen(db_session, seed, b1.id)
    await _reconcile(db_session, seed, _transition(b1.id, ReconciliationState.SKIPPED))
    b1 = await _reload(db_session, b1.id)
    assert (b1.reconciliation_state, b1.linked_transaction_id) == ("skipped", o.id)

    b2 = await _bank(db_session, seed, amount="370.00", paid=MAY_30)
    await _match(db_session, seed, b2.id, o.id)

    o = await _reload(db_session, o.id)
    assert o.amount == Decimal("370.00")
    assert (await _account(db_session, seed["acct_a_id"])).balance == OPENING_A - Decimal("370.00")
    await assert_invariant(db_session, seed)


# ── #10-#14: what is not bindable, and the refusals ─────────────────────────


@pytest.mark.parametrize("cell", ["a_settled_paired", "b_pending_paired", "c_unpaired",
                                  "d_hand_paired"])
async def test_transfer_leg_keeps_todays_match(db_session, cell):
    """#10. Kills: (a) rev 1's 409 refusal of a supported dedupe, or re-pricing
    or re-dating a linked leg; (b) routing a pending leg to today's path; (c) a
    bindable predicate without ``recurring_origin_id``; (d) a bindable predicate
    without ``linked_transaction_id IS NULL``."""
    seed = await _seed(db_session)
    org = seed["org_id"]
    bank_amount = "500.00"
    if cell == "d_hand_paired":
        r = await _template(db_session, seed, amount="500.00", auto_settle=True)
        leg = await _occurrence(db_session, seed, r)
        income = await _create(
            db_session, seed, account_id=seed["acct_b_id"], amount="500.00", label="income",
            tx_type=TransactionType.INCOME, tx_date=JUN_1,
        )
        await transaction_service.pair_existing_transactions(
            db_session, org, leg.id, income.id, recategorize=False,
        )
        leg = await _reload(db_session, leg.id)
        assert leg.recurring_origin_id is None and leg.linked_transaction_id == income.id
        bank_amount = B
    else:
        r = await _template(db_session, seed, amount="500.00", type="transfer",
                            to_account="acct_b_id", auto_settle=(cell != "b_pending_paired"))
        leg = await _occurrence(db_session, seed, r)
        assert leg.type == TransactionType.EXPENSE and leg.recurring_origin_id == r.id
        if cell == "c_unpaired":
            await transaction_service.unpair_transactions(
                db_session, org, leg.id,
                expense_fallback_category_id=seed["cat_id"],
                income_fallback_category_id=seed["cat_id"],
            )
            leg = await _reload(db_session, leg.id)
            assert leg.linked_transaction_id is None and leg.recurring_origin_id == r.id
            bank_amount = B
    partner_id = leg.linked_transaction_id
    bank = await _bank(db_session, seed, amount=bank_amount, paid=MAY_30)
    before = await _snapshot(db_session, seed)
    leg_before = before["rows"][leg.id]

    if cell == "b_pending_paired":
        with pytest.raises(ValidationError) as exc:
            await _match(db_session, seed, bank.id, leg.id)
        assert exc.value.code == "match_target_pending"
        assert await _snapshot(db_session, seed) == before
        return

    await _match(db_session, seed, bank.id, leg.id)

    after = await _snapshot(db_session, seed)
    assert after["rows"][leg.id][:4] == leg_before[:4]  # amount, date, settled_date, status
    if partner_id is not None:
        assert after["rows"][partner_id] == before["rows"][partner_id]
    # The bank row was reverted: its amount left acct_a, nothing else moved.
    assert after["rows"][bank.id][4] == leg.id
    a = seed["acct_a_id"]
    assert after["accounts"][a] == before["accounts"][a] + Decimal(bank_amount)
    assert {k: v for k, v in after["accounts"].items() if k != a} == {
        k: v for k, v in before["accounts"].items() if k != a
    }
    await assert_invariant(db_session, seed)


@pytest.mark.parametrize("cell", ["account_usd", "type_income"])
async def test_account_or_type_mismatch_is_refused(db_session, cell):
    """#11. Kills: no account check (EUR money onto a USD account); no type check."""
    seed = await _seed(db_session)
    if cell == "account_usd":
        acct_b = await _account(db_session, seed["acct_b_id"])
        acct_b.currency = "USD"
        await db_session.commit()
        r = await _template(db_session, seed, account="acct_b_id")
        code = "occurrence_account_mismatch"
    else:
        r = await _template(db_session, seed, type="income")
        code = "occurrence_type_mismatch"
    o = await _occurrence(db_session, seed, r)
    bank = await _bank(db_session, seed)
    before = await _snapshot(db_session, seed)

    with pytest.raises(ValidationError) as exc:
        await _match(db_session, seed, bank.id, o.id)
    assert exc.value.code == code
    assert await _snapshot(db_session, seed) == before


REFUSED = {
    "a_bound", "c_legacy_auto_settled_now_refused", "e_bound_then_stopped",
    "f_bound_then_deleted", "g_bound_then_edited_amount", "h_bound_then_edited_date",
    "i_bound_then_o1_amount_put", "j_dedupe_onto_hand_entered_now_refused",
}


async def _edit_bank(db, seed, bank_id: int, **edits):
    """Reopen, EDIT the bank row, accept, reopen again: back in pending_review."""
    await _reopen(db, seed, bank_id)
    await _reconcile(db, seed, ReconcileBatchRequest(transitions=[ReconciliationTransition(
        transaction_id=bank_id, to_state=ReconciliationState.EDITED,
        edits=ReconciliationEdits(**edits),
    )]))
    await _reopen(db, seed, bank_id)


# Each keeps the re-target; k and l each carry only ONE of the two leg markers,
# so dropping either guard term goes red (d carries both).
# Bound through PR1's match, so PR2's marker is on the bank row.
MARKER_BOUND = {"a_bound", "e_bound_then_stopped", "f_bound_then_deleted"}
TRANSFER_LEG_CELLS = {"d_transfer_leg", "k_hand_paired_leg", "l_unpaired_recurring_leg"}


@pytest.mark.parametrize("cell", [
    "a_bound", "b_legacy_pending", "c_legacy_auto_settled_now_refused", "d_transfer_leg",
    "e_bound_then_stopped", "f_bound_then_deleted", "g_bound_then_edited_amount",
    "h_bound_then_edited_date", "i_bound_then_o1_amount_put",
    "j_dedupe_onto_hand_entered_now_refused", "k_hand_paired_leg",
    "l_unpaired_recurring_leg",
])
async def test_rematch_away_from_a_matched_row(db_session, cell):
    """#12, Insertion A (Option N). Re-pointing a bank row away from a SETTLED
    row the user entered is refused on the row's SHAPE alone, so the refusal
    survives what a value tell does not: stop / delete NULL ``recurring_id``
    (e, f), and an EDITED bank row or a PUT on the occurrence changes the
    amount or date (g, h, i). A PENDING row (b) and a transfer leg keep
    today's re-target: a recurring transfer leg (d), a hand-paired leg with
    no ``recurring_origin_id`` (k), an unpaired leg that keeps it (l).

    PR2: a bind now writes a marker, so (a, e, f) are refused on the marker and
    cannot be skipped (unbind is the way out), and the edits in (g, h, i) are
    themselves refused before any re-target.

    Pinned costs: (c) a pre-TBD-755 match onto an auto-settled occurrence and
    (j) a dedupe onto a hand-entered settled row can no longer be re-targeted.
    Every refused cell then SKIPs the bank row: 200, balance unchanged.
    """
    seed = await _seed(db_session)
    org, a = seed["org_id"], seed["acct_a_id"]

    if cell == "d_transfer_leg":
        xfer = await _template(db_session, seed, amount="500.00", type="transfer",
                               to_account="acct_b_id", auto_settle=True, description="Card")
        r = await _template(db_session, seed)
        await recurring_service.generate_due_transactions(db_session, org, today=JUN_1)
        o1 = await _series_row(db_session, xfer.id, JUN_1)  # the settled leg L
        o2 = await _series_row(db_session, r.id, JUN_1)
        bank = await _bank(db_session, seed, amount="500.00", paid=JUN_1)
        await _match(db_session, seed, bank.id, o1.id)  # today's dedupe
        await _reopen(db_session, seed, bank.id)
    elif cell in ("k_hand_paired_leg", "l_unpaired_recurring_leg"):
        if cell == "k_hand_paired_leg":
            card = await _template(db_session, seed, amount="500.00", auto_settle=True,
                                   description="Card")
        else:
            card = await _template(db_session, seed, amount="500.00", type="transfer",
                                   to_account="acct_b_id", auto_settle=True, description="Card")
        r = await _template(db_session, seed)
        await recurring_service.generate_due_transactions(db_session, org, today=JUN_1)
        o1 = await _series_row(db_session, card.id, JUN_1)
        o2 = await _series_row(db_session, r.id, JUN_1)
        if cell == "k_hand_paired_leg":
            income = await _create(
                db_session, seed, account_id=seed["acct_b_id"], amount="500.00",
                label="income", tx_type=TransactionType.INCOME, tx_date=JUN_1,
            )
            await transaction_service.pair_existing_transactions(
                db_session, org, o1.id, income.id, recategorize=False,
            )
        else:
            await transaction_service.unpair_transactions(
                db_session, org, o1.id,
                expense_fallback_category_id=seed["cat_id"],
                income_fallback_category_id=seed["cat_id"],
            )
        o1 = await _reload(db_session, o1.id)
        assert (o1.linked_transaction_id is None) == (cell == "l_unpaired_recurring_leg")
        assert (o1.recurring_origin_id is None) == (cell == "k_hand_paired_leg")
        bank = await _bank(db_session, seed, amount="500.00", paid=JUN_1)
        await _match(db_session, seed, bank.id, o1.id)  # today's dedupe, no bind
        await _reopen(db_session, seed, bank.id)
    elif cell in ("a_bound", "b_legacy_pending", "c_legacy_auto_settled_now_refused"):
        r = await _template(db_session, seed,
                            auto_settle=(cell == "c_legacy_auto_settled_now_refused"))
        o1 = await _occurrence(db_session, seed, r)
        o2 = await recurring_service.materialise_next(
            db_session, org, r.id, occurrence_date=JUL_1, skipped=False, today=JUN_1,
        )
        assert o2.status == TransactionStatus.PENDING
        bank = await _bank(db_session, seed, paid=JUN_2)
        if cell == "a_bound":
            await _match(db_session, seed, bank.id, o1.id)
            o1 = await _reload(db_session, o1.id)
            assert (o1.status, o1.amount) == (TransactionStatus.SETTLED, Decimal(B))
        else:
            with _legacy_pre_tbd755_match():
                await _match(db_session, seed, bank.id, o1.id)
        await _reopen(db_session, seed, bank.id)
    elif cell == "j_dedupe_onto_hand_entered_now_refused":
        x = await _create(db_session, seed, account_id=a, amount=B, label="hand-entered",
                          tx_date=JUN_2)
        r = await _template(db_session, seed)
        o2 = await _occurrence(db_session, seed, r)
        bank = await _bank(db_session, seed, paid=JUN_2)
        await _match(db_session, seed, bank.id, x.id)  # today's dedupe
        o1 = x
        await _reopen(db_session, seed, bank.id)
    else:  # e-i: bind O1 (Energy), then the event that breaks a value tell
        r = await _template(db_session, seed)
        w = await _template(db_session, seed, amount="50.00", description="Water")
        o1 = await _occurrence(db_session, seed, r)
        o2 = await _series_row(db_session, w.id, JUN_1)
        bank = await _bank(db_session, seed, paid=JUN_2)
        await _match(db_session, seed, bank.id, o1.id)
        if cell in ("e_bound_then_stopped", "f_bound_then_deleted"):
            stop = (recurring_service.stop_recurring if cell == "e_bound_then_stopped"
                    else recurring_service.delete_recurring)
            await stop(db_session, org, r.id)
            o1 = await _reload(db_session, o1.id)
            assert o1 is not None and o1.recurring_id is None
            await _reopen(db_session, seed, bank.id)
        else:
            # PR2: the edit that broke a value tell is itself refused now.
            before = await _snapshot(db_session, seed)
            with pytest.raises(ConflictError) as exc:
                if cell == "g_bound_then_edited_amount":
                    await _edit_bank(db_session, seed, bank.id, amount=Decimal("370.00"))
                elif cell == "h_bound_then_edited_date":
                    await _edit_bank(db_session, seed, bank.id, date=JUN_1)
                else:
                    await transaction_service.update_transaction(
                        db_session, org, o1.id, TransactionUpdate(amount=Decimal("400.00")),
                    )
            assert exc.value.code == (
                "transaction_bound" if cell == "i_bound_then_o1_amount_put" else "bank_row_bound"
            )
            db_session.expunge_all()
            after = await _snapshot(db_session, seed)
            assert after["accounts"] == before["accounts"]
            await assert_invariant(db_session, seed)
            return
    await assert_invariant(db_session, seed)
    o1_id, o2_id, bank_id = o1.id, o2.id, bank.id
    before = await _snapshot(db_session, seed)
    assert before["rows"][bank_id][4] == o1_id
    assert before["rows"][bank_id][5] == "pending_review"

    if cell in REFUSED:
        assert before["rows"][o1_id][3] == TransactionStatus.SETTLED
        with pytest.raises(ConflictError) as exc:
            await _match(db_session, seed, bank_id, o2_id)
        assert exc.value.code == "match_source_bound"
        assert await _snapshot(db_session, seed) == before
        if cell in MARKER_BOUND:
            # PR2: a marker-bound bank row cannot be skipped; the way out is unbind.
            with pytest.raises(ConflictError) as exc:
                await _reconcile(db_session, seed, _transition(bank_id, ReconciliationState.SKIPPED))
            assert exc.value.code == "bank_row_bound"
            db_session.expunge_all()
            assert await _snapshot(db_session, seed) == before
            return
        # The way out the message names: skip the bank row. Nothing moves.
        await _reconcile(db_session, seed, _transition(bank_id, ReconciliationState.SKIPPED))
        after = await _snapshot(db_session, seed)
        assert after["accounts"] == before["accounts"]
        assert after["rows"][bank_id][5] == "skipped"
        await assert_invariant(db_session, seed)
        return

    await _match(db_session, seed, bank_id, o2_id)

    after = await _snapshot(db_session, seed)
    assert after["rows"][o1_id] == before["rows"][o1_id]
    o2 = await _reload(db_session, o2_id)
    assert o2.status == TransactionStatus.SETTLED
    if cell in TRANSFER_LEG_CELLS:
        assert o2.amount == Decimal("500.00")
        assert after["accounts"][a] == OPENING_A - Decimal("1000.00")
    else:
        assert before["rows"][o1_id][3] == TransactionStatus.PENDING
        assert o2.amount == Decimal(B)
        assert after["accounts"][a] == OPENING_A - Decimal(B)
    await assert_invariant(db_session, seed)


async def test_rematch_onto_the_same_bound_occurrence_is_idempotent(db_session):
    """Reopening a bound bank row and matching it to the SAME occurrence is not
    a re-target: no 409, and nothing moves. Kills dropping ``current.id != target.id``."""
    seed = await _seed(db_session)
    r = await _template(db_session, seed)
    o1 = await _occurrence(db_session, seed, r)
    bank = await _bank(db_session, seed, paid=JUN_2)
    await _match(db_session, seed, bank.id, o1.id)
    await _reopen(db_session, seed, bank.id)
    o1_id, bank_id = o1.id, bank.id
    before = await _snapshot(db_session, seed)
    assert before["rows"][o1_id][3] == TransactionStatus.SETTLED

    await _match(db_session, seed, bank_id, o1_id)

    after = await _snapshot(db_session, seed)
    assert after["accounts"] == before["accounts"]
    assert after["rows"][o1_id] == before["rows"][o1_id]
    assert after["rows"][bank_id][4] == o1_id
    await assert_invariant(db_session, seed)


@pytest.mark.parametrize("cell", ["a_pending_occurrence", "b_settled_occurrence"])
async def test_pending_bank_row(db_session, cell):
    """#13. Kills: (a) no check (the balance moves for unpaid money); (b) an
    unconditional refusal of a PENDING bank row."""
    seed = await _seed(db_session)
    r = await _template(db_session, seed, auto_settle=(cell == "b_settled_occurrence"))
    o = await _occurrence(db_session, seed, r)
    bank = await _bank(db_session, seed, status=TransactionStatus.PENDING)
    before = await _snapshot(db_session, seed)

    if cell == "a_pending_occurrence":
        with pytest.raises(ValidationError) as exc:
            await _match(db_session, seed, bank.id, o.id)
        assert exc.value.code == "match_source_not_settled"
        assert await _snapshot(db_session, seed) == before
        return

    await _match(db_session, seed, bank.id, o.id)
    after = await _snapshot(db_session, seed)
    assert after["rows"][o.id] == before["rows"][o.id]
    assert after["rows"][o.id][0] == Decimal(T)
    assert after["accounts"] == before["accounts"]
    await assert_invariant(db_session, seed)


@pytest.mark.parametrize("cell", ["a_pulled_forward", "b_pushed_back"])
async def test_frontier_occurrence_is_not_redated(db_session, cell):
    """#14 (C1). Kills: no frontier check (the row is re-dated to 05-30 and
    generation writes a second Rent on 06-01)."""
    seed = await _seed(db_session)
    rent = await _create(
        db_session, seed, account_id=seed["acct_a_id"], amount=T, label="rent",
        status=TransactionStatus.PENDING, tx_date=JUN_1,
    )
    await transaction_service.promote_to_recurring(
        db_session, seed["org_id"], rent.id,
        PromoteToRecurringRequest(frequency="monthly", next_due_date=JUN_1), today=JUN_1,
    )
    rent = await _reload(db_session, rent.id)
    r = await db_session.get(RecurringTransaction, rent.recurring_id)
    assert (await _frontier(db_session, r))[1] == 0

    if cell == "a_pulled_forward":
        bank = await _bank(db_session, seed, paid=MAY_30)
        before = await _snapshot(db_session, seed)
        with pytest.raises(ConflictError) as exc:
            await _match(db_session, seed, bank.id, rent.id)
        assert exc.value.code == "occurrence_on_frontier"
        assert await _snapshot(db_session, seed) == before
    else:
        bank = await _bank(db_session, seed, paid=JUN_2)
        await _match(db_session, seed, bank.id, rent.id)
        rent = await _reload(db_session, rent.id)
        assert (rent.date, rent.settled_date, rent.amount) == (JUN_1, JUN_2, Decimal(B))

    await recurring_service.generate_due_transactions(db_session, seed["org_id"], today=JUN_1)
    assert await _series_count(db_session, r.id) == 1
    assert (await _frontier(db_session, r))[1] == 1
    await assert_invariant(db_session, seed)


# ── #15-#16: guards ──────────────────────────────────────────────────────────


async def test_counters_match_a_plain_match(db_session):
    """#15 (guard). A bind moves the ImportBatch counters exactly as a plain
    match does, and the batch closes."""
    results = []
    for label, anchor, bindable in (("plain", 7000, False), ("bind", 8000, True)):
        seed = await _seed(db_session, label=label, anchor_id=anchor)
        if bindable:
            r = await _template(db_session, seed)
            target = await _occurrence(db_session, seed, r)
        else:
            target = await _create(db_session, seed, account_id=seed["acct_a_id"],
                                   amount=T, label="plain", tx_date=JUN_1)
        bank = await _bank(db_session, seed)
        await _match(db_session, seed, bank.id, target.id)
        await _reconcile(db_session, seed, _transition(bank.id, ReconciliationState.ACCEPTED))
        batch = await db_session.scalar(
            select(ImportBatch).where(ImportBatch.id == seed["batch_id"])
            .execution_options(populate_existing=True)
        )
        results.append((batch.pending_count, batch.accepted_count, batch.row_count,
                        batch.status.value))
    assert results[0] == results[1]
    assert results[1][3] == "closed"


async def test_bind_leaves_the_currency_scope_untouched(db_session):
    """#16 (guard)."""
    seed = await _seed(db_session)
    org = await db_session.get(Organization, seed["org_id"])
    org.primary_currency = "EUR"
    acct_c = await _account(db_session, seed["acct_c_id"])
    acct_c.currency = "USD"
    await db_session.commit()
    await _create(db_session, seed, account_id=seed["acct_c_id"], amount="64.00",
                  label="usd", tx_date=MAY_29)
    scope = await currency_service.resolve_currency_scope(db_session, org_id=seed["org_id"])
    assert scope["excluded_account_count"] == 1
    r = await _template(db_session, seed)
    o = await _occurrence(db_session, seed, r)
    bank = await _bank(db_session, seed, paid=MAY_29)

    await _match(db_session, seed, bank.id, o.id)

    org = await db_session.scalar(
        select(Organization).where(Organization.id == seed["org_id"])
        .execution_options(populate_existing=True)
    )
    assert org.primary_currency == "EUR"
    after = await currency_service.resolve_currency_scope(db_session, org_id=seed["org_id"])
    assert after == scope
    may = await executed_expense_by_category(
        db_session, seed["org_id"], MAY_1, MAY_31, currency_scope=after,
    )
    assert may == {seed["cat_id"]: Decimal(B)}
