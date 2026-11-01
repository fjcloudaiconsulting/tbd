"""TBD-756: amount and due date sync between a recurring occurrence and its template.

Direction 1 -- ``TransactionUpdate.series_scope="future"`` on a PENDING
occurrence carries its amount and/or date change to the template and to the
PENDING siblings dated AFTER it:

- amount: siblings still at the template's (locked) amount follow; a sibling
  the user deliberately changed keeps its value;
- date: the series is re-walked on its own grid (``advance_date``), never
  shifted by a day count. Sibling k steps after the edited row on the old grid
  lands k steps after the new date; the frontier likewise;
- SETTLED rows, transfer legs, skipped/rejected rows and rows a bank row was
  reconcile-matched against are never touched.

Direction 2 -- an amount change on the template (``update_recurring``) moves
the PENDING occurrences still at the old template amount. A template
``next_due_date`` change moves no materialised row (the frontier is the next
UN-materialised occurrence).

``series_scope`` absent or ``"this"`` keeps the old behaviour: amount and date
stay on the edited row.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.category import CategoryType
from app.models.recurring import Frequency, RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.schemas.recurring import RecurringUpdate
from app.schemas.transaction import TransactionUpdate
from app.services import recurring_service, transaction_service
from app.services.exceptions import ConflictError, ValidationError

pytestmark = pytest.mark.asyncio

# A fixed year well past the current billing cycle, so every date below clears
# the frontier lower bound (TBD-283) whatever day the suite runs on. 2027 has
# no Feb 29, which the month-end cases rely on.
Y = max(date.today().year + 1, 2027)
if Y % 4 == 0:
    Y += 1


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


async def _seed(db: AsyncSession) -> dict:
    org = Organization(name="T", billing_cycle_day=1)
    db.add(org)
    await db.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db.add(at)
    await db.flush()
    acct = Account(
        org_id=org.id, name="Main", account_type_id=at.id,
        balance=Decimal("0"), currency="EUR",
    )
    acct_b = Account(
        org_id=org.id, name="Savings", account_type_id=at.id,
        balance=Decimal("0"), currency="EUR",
    )
    db.add_all([acct, acct_b])
    await db.flush()
    exp = Category(org_id=org.id, name="Lease", slug="lease", type=CategoryType.EXPENSE)
    inc = Category(org_id=org.id, name="Salary", slug="salary", type=CategoryType.INCOME)
    db.add_all([exp, inc])
    await db.commit()
    return {
        "org_id": org.id, "account_id": acct.id, "account_b": acct_b.id,
        "exp_cat": exp.id, "inc_cat": inc.id,
    }


async def _add_template(
    db: AsyncSession, seed: dict, *, next_due: date,
    frequency: Frequency = Frequency.MONTHLY, amount: str = "695.59",
) -> int:
    r = RecurringTransaction(
        org_id=seed["org_id"], account_id=seed["account_id"],
        category_id=seed["exp_cat"], description="Lease", amount=Decimal(amount),
        type="expense", frequency=frequency, next_due_date=next_due,
        auto_settle=False, is_active=True,
    )
    db.add(r)
    await db.commit()
    return r.id


async def _add_row(
    db: AsyncSession, seed: dict, recurring_id: int | None, *, dt: date,
    status: TransactionStatus = TransactionStatus.PENDING, amount: str = "695.59",
    tx_type: TransactionType = TransactionType.EXPENSE, account_id: int | None = None,
    settled_date: date | None = None, reconciliation_state: str = "accepted",
) -> int:
    tx = Transaction(
        org_id=seed["org_id"], account_id=account_id or seed["account_id"],
        category_id=seed["exp_cat"] if tx_type == TransactionType.EXPENSE else seed["inc_cat"],
        description="Lease", amount=Decimal(amount), type=tx_type, status=status,
        date=dt,
        settled_date=(dt if status == TransactionStatus.SETTLED else settled_date),
        recurring_id=recurring_id, reconciliation_state=reconciliation_state,
    )
    db.add(tx)
    await db.commit()
    return tx.id


async def _get_tx(db: AsyncSession, tx_id: int) -> Transaction:
    db.expire_all()
    return await db.get(Transaction, tx_id)


async def _get_tmpl(db: AsyncSession, rid: int) -> RecurringTransaction:
    db.expire_all()
    return await db.get(RecurringTransaction, rid)


async def _edit(db: AsyncSession, seed: dict, tx_id: int, **fields) -> Transaction:
    return await transaction_service.update_transaction(
        db, seed["org_id"], tx_id, TransactionUpdate(**fields),
    )


# ── Direction 1: amount ───────────────────────────────────────────────────────

async def test_future_amount_updates_template_and_later_pending_siblings(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 5, 5))
    settled = await _add_row(db_session, seed, rid, dt=date(Y, 1, 5), status=TransactionStatus.SETTLED)
    earlier = await _add_row(db_session, seed, rid, dt=date(Y, 2, 5))
    edited = await _add_row(db_session, seed, rid, dt=date(Y, 3, 5))
    later = await _add_row(db_session, seed, rid, dt=date(Y, 4, 5))

    await _edit(db_session, seed, edited, amount=Decimal("752.09"), series_scope="future")

    assert (await _get_tx(db_session, edited)).amount == Decimal("752.09")
    assert (await _get_tmpl(db_session, rid)).amount == Decimal("752.09")
    assert (await _get_tx(db_session, later)).amount == Decimal("752.09")
    # Dated before the edited row: "this and future" leaves it alone, and it
    # now reads as differing from the series (TBD-273), which is honest.
    assert (await _get_tx(db_session, earlier)).amount == Decimal("695.59")
    assert (await _get_tx(db_session, settled)).amount == Decimal("695.59")


async def test_future_amount_keeps_a_deliberately_changed_sibling(db_session):
    """An edit-next override (amount != template amount) survives the sync."""
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 6, 5))
    edited = await _add_row(db_session, seed, rid, dt=date(Y, 3, 5))
    untouched = await _add_row(db_session, seed, rid, dt=date(Y, 4, 5))
    overridden = await _add_row(db_session, seed, rid, dt=date(Y, 5, 5), amount="500.00")

    await _edit(db_session, seed, edited, amount=Decimal("752.09"), series_scope="future")

    assert (await _get_tx(db_session, untouched)).amount == Decimal("752.09")
    assert (await _get_tx(db_session, overridden)).amount == Decimal("500.00")


@pytest.mark.parametrize("scope", [None, "this"])
async def test_amount_without_future_scope_stays_on_the_row(db_session, scope):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 5, 5))
    edited = await _add_row(db_session, seed, rid, dt=date(Y, 3, 5))
    later = await _add_row(db_session, seed, rid, dt=date(Y, 4, 5))

    await _edit(db_session, seed, edited, amount=Decimal("752.09"), series_scope=scope)

    assert (await _get_tx(db_session, edited)).amount == Decimal("752.09")
    assert (await _get_tmpl(db_session, rid)).amount == Decimal("695.59")
    assert (await _get_tx(db_session, later)).amount == Decimal("695.59")


async def test_future_amount_with_settle_in_the_same_request(db_session):
    """The common real case: settle at the debited amount and fix the series."""
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 5, 5))
    edited = await _add_row(db_session, seed, rid, dt=date(Y, 3, 5))
    later = await _add_row(db_session, seed, rid, dt=date(Y, 4, 5))

    await _edit(
        db_session, seed, edited, amount=Decimal("752.09"), status="settled",
        settled_date=date(Y, 3, 5), series_scope="future",
    )

    row = await _get_tx(db_session, edited)
    assert row.status == TransactionStatus.SETTLED
    assert row.amount == Decimal("752.09")
    assert (await _get_tmpl(db_session, rid)).amount == Decimal("752.09")
    assert (await _get_tx(db_session, later)).amount == Decimal("752.09")
    acct = await db_session.get(Account, seed["account_id"])
    assert acct.balance == Decimal("-752.09")


async def test_future_amount_skips_transfer_leg_skipped_and_matched_siblings(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 7, 5))
    edited = await _add_row(db_session, seed, rid, dt=date(Y, 2, 5))
    leg = await _add_row(db_session, seed, rid, dt=date(Y, 3, 5))
    partner = await _add_row(
        db_session, seed, None, dt=date(Y, 3, 5), tx_type=TransactionType.INCOME,
        account_id=seed["account_b"],
    )
    skipped = await _add_row(db_session, seed, rid, dt=date(Y, 4, 5), reconciliation_state="skipped")
    matched = await _add_row(db_session, seed, rid, dt=date(Y, 5, 5))
    bank_row = await _add_row(
        db_session, seed, None, dt=date(Y, 5, 6), status=TransactionStatus.SETTLED,
    )
    plain = await _add_row(db_session, seed, rid, dt=date(Y, 6, 5))
    # Links OUT only (nothing links back to it): kills a filter that keeps the
    # referrer EXISTS but drops ``linked_transaction_id IS NULL``. ``matched``
    # is linked IN only, which kills the reverse.
    outgoing = await _add_row(db_session, seed, rid, dt=date(Y, 6, 20))
    leg_row = await db_session.get(Transaction, leg)
    partner_row = await db_session.get(Transaction, partner)
    bank = await db_session.get(Transaction, bank_row)
    outgoing_row = await db_session.get(Transaction, outgoing)
    leg_row.linked_transaction_id = partner
    partner_row.linked_transaction_id = leg
    bank.linked_transaction_id = matched  # one-way reconcile match
    outgoing_row.linked_transaction_id = bank_row
    await db_session.commit()

    await _edit(db_session, seed, edited, amount=Decimal("752.09"), series_scope="future")

    assert (await _get_tx(db_session, leg)).amount == Decimal("695.59")
    assert (await _get_tx(db_session, partner)).amount == Decimal("695.59")
    assert (await _get_tx(db_session, skipped)).amount == Decimal("695.59")
    assert (await _get_tx(db_session, matched)).amount == Decimal("695.59")
    assert (await _get_tx(db_session, outgoing)).amount == Decimal("695.59")
    assert (await _get_tx(db_session, plain)).amount == Decimal("752.09")


async def test_future_amount_skips_a_type_diverged_sibling(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 5, 5))
    edited = await _add_row(db_session, seed, rid, dt=date(Y, 3, 5))
    refund = await _add_row(db_session, seed, rid, dt=date(Y, 4, 5), tx_type=TransactionType.INCOME)

    await _edit(db_session, seed, edited, amount=Decimal("752.09"), series_scope="future")

    assert (await _get_tx(db_session, refund)).amount == Decimal("695.59")


async def test_future_with_no_real_change_is_a_no_op(db_session):
    """The edit form always sends amount and date; re-sending them unchanged
    with "future" must not 400 and must not touch the series."""
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 5, 5))
    edited = await _add_row(db_session, seed, rid, dt=date(Y, 3, 5))
    later = await _add_row(db_session, seed, rid, dt=date(Y, 4, 5), amount="500.00")

    await _edit(
        db_session, seed, edited, amount=Decimal("695.59"), date=date(Y, 3, 5),
        description="Lease", series_scope="future",
    )

    tmpl = await _get_tmpl(db_session, rid)
    assert tmpl.amount == Decimal("695.59")
    assert tmpl.next_due_date == date(Y, 5, 5)
    assert (await _get_tx(db_session, later)).amount == Decimal("500.00")


# ── Direction 1: refusals ─────────────────────────────────────────────────────

async def test_future_refused_on_a_settled_row(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 5, 5))
    edited = await _add_row(db_session, seed, rid, dt=date(Y, 3, 5), status=TransactionStatus.SETTLED)

    with pytest.raises(ValidationError, match="pending"):
        await _edit(db_session, seed, edited, amount=Decimal("752.09"), series_scope="future")


async def test_future_refused_on_a_non_recurring_row(db_session):
    seed = await _seed(db_session)
    edited = await _add_row(db_session, seed, None, dt=date(Y, 3, 5))

    with pytest.raises(ValidationError, match="recurring"):
        await _edit(db_session, seed, edited, amount=Decimal("752.09"), series_scope="future")


async def test_future_refused_on_a_skipped_row(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 5, 5))
    edited = await _add_row(db_session, seed, rid, dt=date(Y, 3, 5), reconciliation_state="skipped")

    with pytest.raises(ValidationError, match="skipped"):
        await _edit(db_session, seed, edited, amount=Decimal("752.09"), series_scope="future")


async def test_future_refused_on_a_transfer_leg(db_session):
    """A leg's amount mirrors its partner, and moving its date strands the
    partner: neither may be pushed onto the whole series."""
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 5, 5))
    edited = await _add_row(db_session, seed, rid, dt=date(Y, 3, 5))
    partner = await _add_row(
        db_session, seed, None, dt=date(Y, 3, 5), tx_type=TransactionType.INCOME,
        account_id=seed["account_b"],
    )
    leg_row = await db_session.get(Transaction, edited)
    partner_row = await db_session.get(Transaction, partner)
    leg_row.linked_transaction_id = partner
    partner_row.linked_transaction_id = edited
    await db_session.commit()

    with pytest.raises(ValidationError, match="linked"):
        await _edit(db_session, seed, edited, date=date(Y, 3, 8), series_scope="future")
    tmpl = await _get_tmpl(db_session, rid)
    assert tmpl.next_due_date == date(Y, 5, 5)


async def test_future_refused_with_a_type_change(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 5, 5))
    edited = await _add_row(db_session, seed, rid, dt=date(Y, 3, 5))

    with pytest.raises(ValidationError, match="type"):
        await _edit(
            db_session, seed, edited, amount=Decimal("752.09"), type="income",
            category_id=seed["inc_cat"], series_scope="future",
        )
    assert (await _get_tmpl(db_session, rid)).amount == Decimal("695.59")


async def test_future_refused_on_a_type_diverged_row(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 5, 5))
    edited = await _add_row(db_session, seed, rid, dt=date(Y, 3, 5), tx_type=TransactionType.INCOME)

    with pytest.raises(ValidationError, match="type"):
        await _edit(db_session, seed, edited, amount=Decimal("752.09"), series_scope="future")


# ── Direction 1: date ─────────────────────────────────────────────────────────

async def test_future_date_moves_template_and_later_siblings_on_the_grid(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 6, 5))
    settled = await _add_row(db_session, seed, rid, dt=date(Y, 2, 5), status=TransactionStatus.SETTLED)
    edited = await _add_row(db_session, seed, rid, dt=date(Y, 3, 5))
    later = await _add_row(db_session, seed, rid, dt=date(Y, 4, 5), settled_date=date(Y, 4, 7))
    later2 = await _add_row(db_session, seed, rid, dt=date(Y, 5, 5))

    await _edit(db_session, seed, edited, date=date(Y, 3, 8), series_scope="future")

    # Moved once, not twice (the row is itself PENDING and dated after its old date).
    assert (await _get_tx(db_session, edited)).date == date(Y, 3, 8)
    later_row = await _get_tx(db_session, later)
    assert later_row.date == date(Y, 4, 8)
    # The expected settlement date keeps its offset from the row's own date.
    assert later_row.settled_date == date(Y, 4, 10)
    assert (await _get_tx(db_session, later2)).date == date(Y, 5, 8)
    assert (await _get_tmpl(db_session, rid)).next_due_date == date(Y, 6, 8)
    assert (await _get_tx(db_session, settled)).date == date(Y, 2, 5)


async def test_future_date_carries_the_edited_rows_expected_settlement(db_session):
    """The form re-sends the row's unchanged expected settlement date. It moves
    with the row, as it does on a sibling, instead of tripping the
    "settled_date must be on or after date" check."""
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 5, 5))
    edited = await _add_row(db_session, seed, rid, dt=date(Y, 3, 5), settled_date=date(Y, 3, 6))

    await _edit(
        db_session, seed, edited, date=date(Y, 3, 8), settled_date=date(Y, 3, 6),
        series_scope="future",
    )

    row = await _get_tx(db_session, edited)
    assert (row.date, row.settled_date) == (date(Y, 3, 8), date(Y, 3, 9))


async def test_future_date_keeps_a_newly_chosen_expected_settlement(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 5, 5))
    edited = await _add_row(db_session, seed, rid, dt=date(Y, 3, 5), settled_date=date(Y, 3, 6))

    await _edit(
        db_session, seed, edited, date=date(Y, 3, 8), settled_date=date(Y, 3, 20),
        series_scope="future",
    )

    assert (await _get_tx(db_session, edited)).settled_date == date(Y, 3, 20)


async def test_future_date_re_walks_month_ends_instead_of_adding_days(db_session):
    """Jan 3 -> Jan 31 is +28 days. A day count would turn Feb 3 into Mar 3
    and empty February; the grid walk puts it on Feb 28."""
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 3, 3))
    edited = await _add_row(db_session, seed, rid, dt=date(Y, 1, 3))
    feb = await _add_row(db_session, seed, rid, dt=date(Y, 2, 3))

    await _edit(db_session, seed, edited, date=date(Y, 1, 31), series_scope="future")

    assert (await _get_tx(db_session, feb)).date == date(Y, 2, 28)
    # advance_date(Feb 28, monthly) -- generation's own iteration.
    assert (await _get_tmpl(db_session, rid)).next_due_date == date(Y, 3, 28)


async def test_future_date_with_amount_moves_both(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 5, 5))
    edited = await _add_row(db_session, seed, rid, dt=date(Y, 3, 5))
    later = await _add_row(db_session, seed, rid, dt=date(Y, 4, 5))

    await _edit(
        db_session, seed, edited, date=date(Y, 3, 8), amount=Decimal("752.09"),
        series_scope="future",
    )

    later_row = await _get_tx(db_session, later)
    assert (later_row.date, later_row.amount) == (date(Y, 4, 8), Decimal("752.09"))
    tmpl = await _get_tmpl(db_session, rid)
    assert (tmpl.next_due_date, tmpl.amount) == (date(Y, 5, 8), Decimal("752.09"))


async def test_future_date_leaves_settled_transfer_and_off_grid_siblings(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 6, 5))
    edited = await _add_row(db_session, seed, rid, dt=date(Y, 2, 5))
    settled_later = await _add_row(db_session, seed, rid, dt=date(Y, 3, 5), status=TransactionStatus.SETTLED)
    off_grid = await _add_row(db_session, seed, rid, dt=date(Y, 4, 20))
    leg = await _add_row(db_session, seed, rid, dt=date(Y, 5, 5))
    partner = await _add_row(
        db_session, seed, None, dt=date(Y, 5, 5), tx_type=TransactionType.INCOME,
        account_id=seed["account_b"],
    )
    leg_row = await db_session.get(Transaction, leg)
    partner_row = await db_session.get(Transaction, partner)
    leg_row.linked_transaction_id = partner
    partner_row.linked_transaction_id = leg
    await db_session.commit()

    await _edit(db_session, seed, edited, date=date(Y, 2, 8), series_scope="future")

    assert (await _get_tx(db_session, settled_later)).date == date(Y, 3, 5)
    assert (await _get_tx(db_session, off_grid)).date == date(Y, 4, 20)
    assert (await _get_tx(db_session, leg)).date == date(Y, 5, 5)
    assert (await _get_tmpl(db_session, rid)).next_due_date == date(Y, 6, 8)


async def test_future_date_refused_for_a_shift_of_one_period_or_more(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 3, 15), frequency=Frequency.WEEKLY)
    edited = await _add_row(db_session, seed, rid, dt=date(Y, 3, 1))

    with pytest.raises(ValidationError, match="less than one"):
        await _edit(db_session, seed, edited, date=date(Y, 3, 8), series_scope="future")
    with pytest.raises(ValidationError, match="less than one"):
        await _edit(db_session, seed, edited, date=date(Y, 2, 22), series_scope="future")
    assert (await _get_tmpl(db_session, rid)).next_due_date == date(Y, 3, 15)
    assert (await _get_tx(db_session, edited)).date == date(Y, 3, 1)


async def test_future_date_refused_before_the_current_billing_cycle(db_session):
    seed = await _seed(db_session)
    p_start = await recurring_service.frontier_lower_bound(db_session, seed["org_id"])
    rid = await _add_template(
        db_session, seed, next_due=p_start + timedelta(days=16), frequency=Frequency.WEEKLY,
    )
    edited = await _add_row(db_session, seed, rid, dt=p_start + timedelta(days=2))

    with pytest.raises(ValidationError, match="billing cycle"):
        await _edit(
            db_session, seed, edited, date=p_start - timedelta(days=1), series_scope="future",
        )


async def test_future_date_refused_when_a_moved_row_lands_on_another_occurrence(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 3, 15), frequency=Frequency.WEEKLY)
    edited = await _add_row(db_session, seed, rid, dt=date(Y, 3, 1))
    await _add_row(db_session, seed, rid, dt=date(Y, 3, 8))
    # A settled row the user had moved off the grid, sitting on Mar 11.
    await _add_row(db_session, seed, rid, dt=date(Y, 3, 11), status=TransactionStatus.SETTLED)

    with pytest.raises(ConflictError, match="already has"):
        await _edit(db_session, seed, edited, date=date(Y, 3, 4), series_scope="future")
    assert (await _get_tmpl(db_session, rid)).next_due_date == date(Y, 3, 15)


async def test_future_date_refused_when_the_frontier_would_fall_behind_a_row(db_session):
    """The frontier must stay after every row of the series, or generation and
    the forecast count that period twice (the (recurring_id, date) probe)."""
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 3, 8), frequency=Frequency.WEEKLY)
    edited = await _add_row(db_session, seed, rid, dt=date(Y, 3, 1))
    await _add_row(db_session, seed, rid, dt=date(Y, 3, 6), status=TransactionStatus.SETTLED)

    with pytest.raises(ConflictError):
        await _edit(db_session, seed, edited, date=date(Y, 2, 26), series_scope="future")
    assert (await _get_tmpl(db_session, rid)).next_due_date == date(Y, 3, 8)


async def test_future_date_refused_when_the_series_is_off_this_rows_schedule(db_session):
    """The template's frontier is not on the grid walked from this row (its
    schedule was moved separately), so there is no honest mapping."""
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 4, 7))
    edited = await _add_row(db_session, seed, rid, dt=date(Y, 3, 5))

    with pytest.raises(ValidationError, match="schedule"):
        await _edit(db_session, seed, edited, date=date(Y, 3, 8), series_scope="future")
    assert (await _get_tmpl(db_session, rid)).next_due_date == date(Y, 4, 7)


# ── Direction 2: template -> occurrences ──────────────────────────────────────

async def test_template_amount_moves_untouched_pending_occurrences(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 6, 5))
    settled = await _add_row(db_session, seed, rid, dt=date(Y, 2, 5), status=TransactionStatus.SETTLED)
    untouched = await _add_row(db_session, seed, rid, dt=date(Y, 3, 5))
    overridden = await _add_row(db_session, seed, rid, dt=date(Y, 4, 5), amount="500.00")
    skipped = await _add_row(db_session, seed, rid, dt=date(Y, 5, 5), reconciliation_state="skipped")
    refund = await _add_row(db_session, seed, rid, dt=date(Y, 5, 10), tx_type=TransactionType.INCOME)
    leg = await _add_row(db_session, seed, rid, dt=date(Y, 5, 15))
    partner = await _add_row(
        db_session, seed, None, dt=date(Y, 5, 15), tx_type=TransactionType.INCOME,
        account_id=seed["account_b"],
    )
    matched = await _add_row(db_session, seed, rid, dt=date(Y, 5, 20))
    bank_row = await _add_row(
        db_session, seed, None, dt=date(Y, 5, 21), status=TransactionStatus.SETTLED,
    )
    leg_row = await db_session.get(Transaction, leg)
    partner_row = await db_session.get(Transaction, partner)
    bank = await db_session.get(Transaction, bank_row)
    leg_row.linked_transaction_id = partner
    partner_row.linked_transaction_id = leg
    bank.linked_transaction_id = matched
    await db_session.commit()

    await recurring_service.update_recurring(
        db_session, seed["org_id"], rid, RecurringUpdate(amount=Decimal("752.09")),
    )

    assert (await _get_tmpl(db_session, rid)).amount == Decimal("752.09")
    assert (await _get_tx(db_session, untouched)).amount == Decimal("752.09")
    assert (await _get_tx(db_session, overridden)).amount == Decimal("500.00")
    assert (await _get_tx(db_session, skipped)).amount == Decimal("695.59")
    assert (await _get_tx(db_session, settled)).amount == Decimal("695.59")
    assert (await _get_tx(db_session, refund)).amount == Decimal("695.59")
    assert (await _get_tx(db_session, leg)).amount == Decimal("695.59")
    assert (await _get_tx(db_session, matched)).amount == Decimal("695.59")


async def test_template_next_due_date_change_moves_no_materialised_row(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 4, 5))
    pending = await _add_row(db_session, seed, rid, dt=date(Y, 3, 5))

    await recurring_service.update_recurring(
        db_session, seed["org_id"], rid, RecurringUpdate(next_due_date=date(Y, 4, 8)),
    )

    assert (await _get_tx(db_session, pending)).date == date(Y, 3, 5)
