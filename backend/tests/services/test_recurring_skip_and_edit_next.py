"""Skip one occurrence (TBD-272) and edit the next one's amount (TBD-273).

Ruling (Jira, 2026-09-13): act AT THE FRONTIER. ``materialise_next`` writes
the template's ``next_due_date`` occurrence with generation's own create step
and spends it; a skip writes that row PENDING in ``reconciliation_state
'skipped'``. ``skip_occurrence`` marks an already-generated PENDING row
skipped. Nothing is ever written ahead of the frontier.

Each test is labelled ``fence`` (goes RED under the named wrong implementation,
injected and confirmed) or ``guard``. Amounts are deliberately asymmetric so a
sign flip or a doubled term cannot cancel out.

Clocks are injected everywhere; fixtures anchor to ``date.today() - n`` via
``_safe_month_anchor`` so the grid never lands on a day-of-month that does not
exist in every month.

⚠ Concurrency (the lock plus the post-lock existence check serialising
``materialise_next`` with generation) is NOT provable on sqlite and nothing
here claims to prove it.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from dateutil.relativedelta import relativedelta
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.billing import BillingPeriod
from app.models.category import CategoryType
from app.models.import_batch import ImportBatch, ImportBatchStatus, ImportSourceFormat
from app.models.recurring import RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import User
from app.schemas.import_reconciliation import (
    ReconcileBatchRequest,
    ReconciliationState,
    ReconciliationTransition,
)
from app.schemas.recurring import RecurringUpdate
from app.schemas.transaction import TransactionUpdate
from app.services import (
    forecast_service,
    reconciliation_service,
    recurring_service,
    transaction_service,
)
from app.services.account_balance_forecast_service import (
    compute_account_balance_forecast,
)
from app.services.billing_service import current_cycle_window
from app.services.exceptions import ConflictError, ValidationError

DAY = datetime.timedelta(days=1)


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(Engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


# ─── scaffolding ──────────────────────────────────────────────────────────────

def _safe_month_anchor(d: datetime.date) -> datetime.date:
    while d.day > 28:
        d -= DAY
    return d


async def _seed(db: AsyncSession, today: datetime.date) -> dict:
    """One org, one account (balance 1000), one expense category.

    P1 is OPEN and is generation's window at ``today``; P2 is its CLOSED
    successor, so a frontier can sit in a period that has not started yet.
    """
    p_start = _safe_month_anchor(today - 10 * DAY)
    org = Organization(name="T", billing_cycle_day=p_start.day)
    db.add(org)
    await db.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db.add(at)
    await db.flush()
    acct = Account(
        org_id=org.id, name="Main", account_type_id=at.id,
        balance=Decimal("1000.00"), currency="EUR", is_default=True,
    )
    db.add(acct)
    await db.flush()
    cat = Category(org_id=org.id, name="Rent", slug="rent", type=CategoryType.EXPENSE)
    db.add(cat)
    await db.flush()
    p1 = (p_start, p_start + relativedelta(months=1) - DAY)
    p2 = (p1[1] + DAY, p1[1] + DAY + relativedelta(months=1) - DAY)
    db.add(BillingPeriod(org_id=org.id, start_date=p1[0]))
    db.add(BillingPeriod(org_id=org.id, start_date=p2[0], end_date=p2[1]))
    await db.commit()
    if current_cycle_window(p_start.day, today) != p1:
        pytest.fail(f"cycle window drifted from P1 {p1}")
    return {
        "org_id": org.id, "account_id": acct.id, "cat_id": cat.id,
        "p1": p1, "p2": p2,
    }


async def _template(db: AsyncSession, seed: dict, **overrides) -> RecurringTransaction:
    fields = dict(
        org_id=seed["org_id"], account_id=seed["account_id"],
        category_id=seed["cat_id"], description="rent",
        amount=Decimal("37.00"), type="expense", frequency="weekly",
        auto_settle=False, is_active=True, occurrences_elapsed=0,
    )
    fields.update(overrides)
    r = RecurringTransaction(**fields)
    db.add(r)
    await db.commit()
    return r


async def _reload_template(db: AsyncSession, rid: int) -> RecurringTransaction:
    return (await db.execute(
        select(RecurringTransaction).where(RecurringTransaction.id == rid)
        .execution_options(populate_existing=True)
    )).scalar_one()


async def _rows(db: AsyncSession, org_id: int) -> list[Transaction]:
    return list((await db.execute(
        select(Transaction).where(Transaction.org_id == org_id)
        .order_by(Transaction.date, Transaction.id)
        .execution_options(populate_existing=True)
    )).scalars().all())


async def _balance(db: AsyncSession, account_id: int) -> Decimal:
    return (await db.execute(
        select(Account.balance).where(Account.id == account_id)
        .execution_options(populate_existing=True)
    )).scalar_one()


async def _net(db, seed, period: str, today) -> Decimal:
    fc = await forecast_service.compute_forecast(
        db, seed["org_id"], period_start=seed[period][0], today=today
    )
    return Decimal(fc["forecast_net"])


async def _acct_fc(db, seed, period: str, today) -> dict:
    fc = await compute_account_balance_forecast(
        db, seed["org_id"], period_start=seed[period][0], today=today
    )
    (acct,) = [a for a in fc["accounts"] if a["account_id"] == seed["account_id"]]
    return acct


# ─────────────────────────────────────────────────────────────────────────────
# S1 — fence. Both forecasts drop the skipped occurrence, and generation agrees.
# ─────────────────────────────────────────────────────────────────────────────

async def test_s1_skip_next_drops_the_occurrence_from_both_forecasts(db_session):
    """FENCE.

    Wrong implementations killed:
      * a skip only generation can see (e.g. advancing the frontier without
        writing a visible row, or a marker the forecast does not read) --
        the skipped row must exist and both projections must drop it;
      * fix (b) missing: the account balance forecast's pending query counts
        the skipped PENDING row, so ``pending_delta`` goes to -37 and the
        month-end balance double-drops.
    """
    today = datetime.date.today()
    seed = await _seed(db_session, today)
    p2_start, _ = seed["p2"]
    frontier = p2_start + 2 * DAY
    r = await _template(db_session, seed, next_due_date=frontier)

    net_before = await _net(db_session, seed, "p2", today)
    acct_before = await _acct_fc(db_session, seed, "p2", today)
    assert {"amount": "-37.00", "date": frontier.isoformat()} in acct_before["recurring_lines"]

    tx = await recurring_service.materialise_next(
        db_session, seed["org_id"], r.id, skipped=True, today=today
    )
    assert tx.date == frontier
    assert tx.reconciliation_state == "skipped"
    assert tx.status == TransactionStatus.PENDING

    net_skip = await _net(db_session, seed, "p2", today)
    acct_skip = await _acct_fc(db_session, seed, "p2", today)
    assert net_skip - net_before == Decimal("37.00")
    assert frontier.isoformat() not in {l["date"] for l in acct_skip["recurring_lines"]}
    assert Decimal(acct_skip["pending_delta"]) == Decimal("0.00")
    assert (
        Decimal(acct_skip["expected_month_end_balance"])
        - Decimal(acct_before["expected_month_end_balance"])
        == Decimal("37.00")
    )

    # Generation at P2's own clock: no active row for the skipped date, and
    # neither forecast moves across the run.
    today2 = p2_start + 20 * DAY
    net_pre = await _net(db_session, seed, "p2", today2)
    acct_pre = await _acct_fc(db_session, seed, "p2", today2)
    await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=today2
    )
    net_post = await _net(db_session, seed, "p2", today2)
    acct_post = await _acct_fc(db_session, seed, "p2", today2)
    assert net_post == net_pre
    assert acct_post["expected_month_end_balance"] == acct_pre["expected_month_end_balance"]

    at_frontier = [t for t in await _rows(db_session, seed["org_id"]) if t.date == frontier]
    assert [(t.id, t.reconciliation_state) for t in at_frontier] == [(tx.id, "skipped")]
    # Anti-vacuity: generation did create the later occurrences.
    assert any(
        t.date > frontier and t.reconciliation_state == "accepted"
        for t in await _rows(db_session, seed["org_id"])
    )
    assert await _balance(db_session, seed["account_id"]) == Decimal("1000.00")


# ─────────────────────────────────────────────────────────────────────────────
# S2 — fence. A skip spends an instalment.
# ─────────────────────────────────────────────────────────────────────────────

async def test_s2_skip_spends_an_instalment(db_session):
    """FENCE. ``occurrence_count=3``: 2 active rows + 1 skipped, elapsed == 3.

    Wrong implementations killed:
      * ``materialise_next`` not calling ``_advance_frontier`` (the frontier
        and the counter must move AT the skip, not later via generation's
        ``exists`` branch);
      * a skip that does not spend budget -- generation would then write 3
        active rows.
    """
    today = datetime.date.today()
    seed = await _seed(db_session, today)
    p_start, _ = seed["p1"]
    f0 = p_start + DAY
    r = await _template(
        db_session, seed, next_due_date=f0, occurrence_count=3, amount=Decimal("23.00"),
    )

    await recurring_service.materialise_next(
        db_session, seed["org_id"], r.id, skipped=True, today=today
    )
    r = await _reload_template(db_session, r.id)
    assert r.occurrences_elapsed == 1
    assert r.next_due_date == f0 + 7 * DAY

    await recurring_service.generate_due_transactions(db_session, seed["org_id"], today=today)
    r = await _reload_template(db_session, r.id)
    rows = await _rows(db_session, seed["org_id"])
    assert [(t.date, t.reconciliation_state) for t in rows] == [
        (f0, "skipped"), (f0 + 7 * DAY, "accepted"), (f0 + 14 * DAY, "accepted"),
    ]
    assert r.occurrences_elapsed == 3


# ─────────────────────────────────────────────────────────────────────────────
# S3 — fence. An auto-settle skip never settles and never moves the balance.
# ─────────────────────────────────────────────────────────────────────────────

async def test_s3_auto_settle_skip_stays_pending_and_balance_untouched(db_session):
    """FENCE.

    Wrong implementations killed:
      * the skip reusing generation's settle-and-apply branch (auto_settle on,
        ``due <= today``): the row is written SETTLED and the balance drops 41;
      * fix (a) missing: ``_settle_due_auto`` promotes the skipped PENDING row
        on the next generation run and applies it to the balance.
    """
    today = datetime.date.today()
    seed = await _seed(db_session, today)
    frontier = today - 2 * DAY
    r = await _template(
        db_session, seed, next_due_date=frontier, auto_settle=True, amount=Decimal("41.00"),
    )

    tx = await recurring_service.materialise_next(
        db_session, seed["org_id"], r.id, skipped=True, today=today
    )
    assert tx.status == TransactionStatus.PENDING
    assert tx.settled_date is None
    assert await _balance(db_session, seed["account_id"]) == Decimal("1000.00")

    await recurring_service.generate_due_transactions(db_session, seed["org_id"], today=today)
    (skipped,) = [t for t in await _rows(db_session, seed["org_id"]) if t.id == tx.id]
    assert skipped.status == TransactionStatus.PENDING
    assert skipped.settled_date is None
    assert await _balance(db_session, seed["account_id"]) == Decimal("1000.00")


# ─────────────────────────────────────────────────────────────────────────────
# S4 — guards (wire status codes are fenced in the route file).
# ─────────────────────────────────────────────────────────────────────────────

async def test_s4_row_already_at_frontier_is_refused_without_change(db_session):
    """FENCE. Kills a missing post-lock existence check: a second row at the
    same ``(recurring_id, date)`` would be written and an instalment spent."""
    today = datetime.date.today()
    seed = await _seed(db_session, today)
    frontier = today + 3 * DAY
    r = await _template(db_session, seed, next_due_date=frontier, occurrence_count=5)
    db_session.add(Transaction(
        org_id=seed["org_id"], account_id=seed["account_id"], category_id=seed["cat_id"],
        description="rent", amount=Decimal("37.00"), type=TransactionType.EXPENSE,
        status=TransactionStatus.PENDING, date=frontier, recurring_id=r.id,
    ))
    await db_session.commit()
    rid = r.id

    for skipped in (True, False):
        with pytest.raises(ConflictError):
            await recurring_service.materialise_next(
                db_session, seed["org_id"], rid, skipped=skipped, today=today
            )
        await db_session.rollback()
    r = await _reload_template(db_session, rid)
    assert (r.next_due_date, r.occurrences_elapsed) == (frontier, 0)
    assert len(await _rows(db_session, seed["org_id"])) == 1


@pytest.mark.parametrize("case", ["inactive", "exhausted", "before_cycle_start"])
async def test_s4_state_guards_refuse_without_change(db_session, case):
    """FENCE. Kills a missing active / remaining-budget / cycle-start guard:
    each would write a row (a closed-period write in the last case)."""
    today = datetime.date.today()
    seed = await _seed(db_session, today)
    p_start, _ = seed["p1"]
    kwargs = {
        "inactive": dict(next_due_date=today + DAY, is_active=False),
        "exhausted": dict(next_due_date=today + DAY, occurrence_count=2, occurrences_elapsed=2),
        "before_cycle_start": dict(next_due_date=p_start - 3 * DAY),
    }[case]
    rid = (await _template(db_session, seed, **kwargs)).id
    with pytest.raises(ConflictError):
        await recurring_service.materialise_next(
            db_session, seed["org_id"], rid, skipped=True, today=today
        )
    await db_session.rollback()
    r = await _reload_template(db_session, rid)
    assert r.next_due_date == kwargs["next_due_date"]
    assert await _rows(db_session, seed["org_id"]) == []


# ─────────────────────────────────────────────────────────────────────────────
# S5 — skip an EXISTING occurrence.
# ─────────────────────────────────────────────────────────────────────────────

async def test_s5_skip_existing_pending_row(db_session):
    """FENCE. The row becomes skipped, the balance does not move, and it leaves
    the pending terms of BOTH forecasts. Kills a skip that changes a flag the
    aggregates do not read, and fix (b) missing."""
    today = datetime.date.today()
    seed = await _seed(db_session, today)
    r = await _template(db_session, seed, next_due_date=today + 2 * DAY, amount=Decimal("19.00"))
    await recurring_service.generate_due_transactions(db_session, seed["org_id"], today=today)
    target = (await _rows(db_session, seed["org_id"]))[0]
    assert target.date == today + 2 * DAY
    frontier_before = (await _reload_template(db_session, r.id)).next_due_date

    fc_before = await forecast_service.compute_forecast(db_session, seed["org_id"], period_start=seed["p1"][0], today=today)
    acct_before = await _acct_fc(db_session, seed, "p1", today)

    tx = await recurring_service.skip_occurrence(db_session, seed["org_id"], target.id)
    assert tx.reconciliation_state == "skipped"
    assert tx.status == TransactionStatus.PENDING
    assert await _balance(db_session, seed["account_id"]) == Decimal("1000.00")
    assert (await _reload_template(db_session, r.id)).next_due_date == frontier_before

    fc_after = await forecast_service.compute_forecast(db_session, seed["org_id"], period_start=seed["p1"][0], today=today)
    acct_after = await _acct_fc(db_session, seed, "p1", today)
    assert Decimal(fc_before["pending_expense"]) - Decimal(fc_after["pending_expense"]) == Decimal("19.00")
    assert Decimal(acct_after["pending_delta"]) - Decimal(acct_before["pending_delta"]) == Decimal("19.00")


@pytest.mark.parametrize(
    "case", ["settled", "no_recurring_id", "already_skipped", "linked", "link_target", "imported"],
)
async def test_s5_skip_existing_refusals_leave_row_unchanged(db_session, case):
    """FENCE. Each refusal kills the missing guard of the same name: a settled
    row's amount is inside the balance; a non-recurring row is not an
    occurrence; a linked row in either direction is a transfer leg or a
    reconcile match; an import-batch row would strand the batch counters."""
    today = datetime.date.today()
    seed = await _seed(db_session, today)
    r = await _template(db_session, seed, next_due_date=today + 2 * DAY)

    def _tx(**kw) -> Transaction:
        fields = dict(
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=seed["cat_id"], description="rent", amount=Decimal("37.00"),
            type=TransactionType.EXPENSE, status=TransactionStatus.PENDING,
            date=today + DAY, recurring_id=r.id,
        )
        fields.update(kw)
        return Transaction(**fields)

    target = _tx()
    if case == "settled":
        target = _tx(status=TransactionStatus.SETTLED, settled_date=today + DAY)
    elif case == "no_recurring_id":
        target = _tx(recurring_id=None)
    elif case == "already_skipped":
        target = _tx(reconciliation_state="skipped")
    db_session.add(target)
    await db_session.flush()
    if case == "linked":
        other = _tx(recurring_id=None, date=today)
        db_session.add(other)
        await db_session.flush()
        target.linked_transaction_id = other.id
    elif case == "link_target":
        db_session.add(_tx(recurring_id=None, date=today, linked_transaction_id=target.id))
    elif case == "imported":
        user = User(username="u", email="u@x.io", password_hash="x", org_id=seed["org_id"])
        db_session.add(user)
        await db_session.flush()
        batch = ImportBatch(
            org_id=seed["org_id"], account_id=seed["account_id"],
            source_format=ImportSourceFormat.CSV, file_name="s.csv",
            created_by_user_id=user.id, status=ImportBatchStatus.CLOSED,
            row_count=1, accepted_count=1, pending_count=0,
        )
        db_session.add(batch)
        await db_session.flush()
        target.import_batch_id = batch.id
    await db_session.commit()
    snapshot = (target.status, target.reconciliation_state, target.linked_transaction_id)

    with pytest.raises(ConflictError):
        await recurring_service.skip_occurrence(db_session, seed["org_id"], target.id)
    await db_session.rollback()
    (after,) = [t for t in await _rows(db_session, seed["org_id"]) if t.id == target.id]
    assert (after.status, after.reconciliation_state, after.linked_transaction_id) == snapshot


# ─────────────────────────────────────────────────────────────────────────────
# S6 / S7 — the two write paths a skipped row must refuse.
# ─────────────────────────────────────────────────────────────────────────────

async def test_s6_apply_match_refuses_a_skipped_target(db_session):
    """FENCE. Kills fix (d) missing: the bank row would be linked onto a row
    whose amount was never in the balance, and leave every aggregate."""
    today = datetime.date.today()
    seed = await _seed(db_session, today)
    r = await _template(db_session, seed, next_due_date=today + 2 * DAY)
    skipped = await recurring_service.materialise_next(
        db_session, seed["org_id"], r.id, skipped=True, today=today
    )
    user = User(username="u", email="u@x.io", password_hash="x", org_id=seed["org_id"])
    db_session.add(user)
    await db_session.flush()
    batch = ImportBatch(
        org_id=seed["org_id"], account_id=seed["account_id"],
        source_format=ImportSourceFormat.CSV, file_name="s.csv",
        created_by_user_id=user.id, status=ImportBatchStatus.OPEN,
        row_count=1, accepted_count=0, pending_count=1,
    )
    db_session.add(batch)
    await db_session.flush()
    bank = Transaction(
        org_id=seed["org_id"], account_id=seed["account_id"], category_id=seed["cat_id"],
        description="RENT CO", amount=Decimal("37.00"), type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED, date=today, settled_date=today,
        is_imported=True, import_batch_id=batch.id, reconciliation_state="pending_review",
    )
    db_session.add(bank)
    await db_session.commit()

    with pytest.raises(ValidationError):
        await reconciliation_service.reconcile_request(
            db_session, org_id=seed["org_id"], batch_id=batch.id,
            request=ReconcileBatchRequest(transitions=[ReconciliationTransition(
                transaction_id=bank.id, to_state=ReconciliationState.MATCHED,
                match_with_transaction_id=skipped.id,
            )]),
        )
    await db_session.rollback()
    (after,) = [t for t in await _rows(db_session, seed["org_id"]) if t.id == bank.id]
    assert (after.linked_transaction_id, after.reconciliation_state) == (None, "pending_review")


async def test_s7_date_edit_on_a_skipped_row_is_refused(db_session):
    """FENCE. Kills fix (e) missing: moving a skipped row onto another grid
    date would suppress a real occurrence (TBD-271 exception 3)."""
    today = datetime.date.today()
    seed = await _seed(db_session, today)
    r = await _template(db_session, seed, next_due_date=today + 2 * DAY)
    skipped = await recurring_service.materialise_next(
        db_session, seed["org_id"], r.id, skipped=True, today=today
    )
    with pytest.raises(ValidationError):
        await transaction_service.update_transaction(
            db_session, seed["org_id"], skipped.id,
            TransactionUpdate(date=today + 9 * DAY),
        )
    await db_session.rollback()
    (after,) = [t for t in await _rows(db_session, seed["org_id"]) if t.id == skipped.id]
    assert after.date == today + 2 * DAY


# ─────────────────────────────────────────────────────────────────────────────
# E1 — fence. Edit next: materialise, then an ordinary amount edit.
# ─────────────────────────────────────────────────────────────────────────────

async def test_e1_edit_next_amount_moves_forecast_by_the_delta_only(db_session):
    """FENCE.

    Wrong implementations killed:
      * the edit propagating to the template (``template.amount`` changes, and
        every later occurrence would move) -- argued, not injected: no such
        path exists in ``update_transaction`` to break;
      * materialise-next double counting (``forecast_net`` moves at the
        materialise, or across generation);
      * ``differs_from_series`` ignoring the reverted term (a skipped row with
        a different amount would read true), or never true.
    """
    today = datetime.date.today()
    seed = await _seed(db_session, today)
    p2_start, _ = seed["p2"]
    frontier = p2_start + 2 * DAY
    r = await _template(db_session, seed, next_due_date=frontier)

    net0 = await _net(db_session, seed, "p2", today)
    row = await recurring_service.materialise_next(
        db_session, seed["org_id"], r.id, skipped=False, today=today
    )
    assert (row.status, row.reconciliation_state) == (TransactionStatus.PENDING, "accepted")
    assert await _net(db_session, seed, "p2", today) == net0

    await transaction_service.update_transaction(
        db_session, seed["org_id"], row.id, TransactionUpdate(amount=Decimal("52.00"))
    )
    net_edit = await _net(db_session, seed, "p2", today)
    assert net0 - net_edit == Decimal("15.00")
    assert (await _reload_template(db_session, r.id)).amount == Decimal("37.00")

    today2 = p2_start + 20 * DAY
    net_pre = await _net(db_session, seed, "p2", today2)
    await recurring_service.generate_due_transactions(db_session, seed["org_id"], today=today2)
    assert await _net(db_session, seed, "p2", today2) == net_pre

    # A sibling edited to a different amount and then skipped: reverted rows
    # never read as differing.
    sibling, skipped_sibling = [
        t for t in await _rows(db_session, seed["org_id"]) if t.date > frontier
    ][:2]
    await transaction_service.update_transaction(
        db_session, seed["org_id"], skipped_sibling.id, TransactionUpdate(amount=Decimal("60.00"))
    )
    await recurring_service.skip_occurrence(db_session, seed["org_id"], skipped_sibling.id)

    items, _ = await transaction_service.list_transactions(db_session, seed["org_id"], limit=50)
    flags = {tx.id: transaction_service.to_response(tx).differs_from_series for tx in items}
    assert flags[row.id] is True
    assert flags[sibling.id] is False
    assert flags[skipped_sibling.id] is False


# ─────────────────────────────────────────────────────────────────────────────
# Stop / resume — fence.
# ─────────────────────────────────────────────────────────────────────────────

async def test_stop_after_skip_next_then_resume_does_not_recreate_it(db_session):
    """FENCE. Stop deletes the future skipped row; resume must not bring the
    occurrence back. Kills a skip that leaves the frontier on the skipped date
    (generation would re-create it once stop has deleted the row)."""
    today = datetime.date.today()
    seed = await _seed(db_session, today)
    frontier = today + 3 * DAY
    r = await _template(db_session, seed, next_due_date=frontier, amount=Decimal("29.00"))
    await recurring_service.materialise_next(
        db_session, seed["org_id"], r.id, skipped=True, today=today
    )

    outcome = await recurring_service.stop_recurring(db_session, seed["org_id"], r.id)
    assert outcome.removed == 1
    assert await _rows(db_session, seed["org_id"]) == []

    await recurring_service.update_recurring(
        db_session, seed["org_id"], r.id, RecurringUpdate(is_active=True), today=today
    )
    await recurring_service.generate_due_transactions(db_session, seed["org_id"], today=today)
    rows = await _rows(db_session, seed["org_id"])
    assert frontier not in {t.date for t in rows}
    assert rows, "anti-vacuity: generation ran and wrote the later occurrences"
