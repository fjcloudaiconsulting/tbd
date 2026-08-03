"""Stop then Resume must not back-fill the paused gap (TBD-300).

## The defect this fences

``stop_recurring`` sets ``is_active = False`` and **freezes** ``next_due_date``
-- it does not advance it. ``generate_due_transactions`` filters on
``is_active == True``, so while a template is stopped its frontier does not
move: it falls one day further behind for every day paused.

``handleResume`` (``frontend/app/recurring/page.tsx``) then sends
``{is_active: true}`` and nothing else -- no date, and no UI affordance to
supply one. On the next scheduler tick the catch-up loop materialises **every
occurrence between the freeze point and today**, each one a money row, each one
written SETTLED when ``auto_settle`` is on, each one applying to the account
balance. Two clicks, silent.

## ⚠ The fixture MUST have a real time gap

The frontier only drifts *while time passes*. A fixture that generates, stops
and resumes at a single fixed ``today`` never opens a gap at all: generation
always advances ``next_due_date`` past everything it creates, so the frontier
ends up AHEAD of ``p_start`` and there is nothing to re-anchor.

An earlier version of this file did exactly that, and **all five fences were
green against every wrong implementation, including unmodified `main`.** Hence
the two clocks below. If you touch this fixture, re-run the injection gate.

## The rule (architect ruling, TBD-300)

Reactivating a template advances its frontier along its own ``advance_date``
grid to the first occurrence ``>= p_start`` (current billing cycle start).
Resume does **not** back-fill the paused gap.

``p_start`` rather than ``today`` on purpose: a template resumed mid-cycle must
still produce the current cycle's occurrence. Advancing to ``today`` silently
skips a charge the user is genuinely due.

⚠ Along the grid, never ``next_due_date = today``. The latter re-anchors the
series -- a rent template paused on the 1st and resumed on the 17th would bill
on the 17th forever, which is worse because it is invisible.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.category import CategoryType
from app.models.recurring import RecurringTransaction
from app.models.transaction import Transaction, TransactionType
from app.schemas.recurring import RecurringUpdate
from app.services import recurring_service
from app.services.recurring_service import Frequency


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


# Fixed literals, not clock offsets: the quantity under test is a COUNT of
# monthly occurrences between two dates, which swings with day-of-month if the
# anchor moves (TBD-278 / reference_wall_clock_date_bomb_tests).
#
# T0 -> generate + stop.  T1 -> resume + generate.  The five-month gap between
# them is the whole point; see the module docstring.
T0 = datetime.date(2026, 1, 20)
T1 = datetime.date(2026, 6, 20)
CYCLE_DAY = 1
P_START_AT_T1 = datetime.date(2026, 6, 1)
FIRST_DUE = datetime.date(2026, 1, 5)
AMOUNT = Decimal("100.00")


async def _seed(db: AsyncSession) -> dict:
    org = Organization(name="T", billing_cycle_day=CYCLE_DAY)
    db.add(org)
    await db.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db.add(at)
    await db.flush()
    acct = Account(
        org_id=org.id, name="Main", account_type_id=at.id,
        balance=Decimal("0.00"), opening_balance=Decimal("0.00"), currency="EUR",
    )
    db.add(acct)
    await db.flush()
    cat = Category(org_id=org.id, name="Rent", slug="rent", type=CategoryType.EXPENSE)
    db.add(cat)
    await db.flush()
    tpl = RecurringTransaction(
        org_id=org.id, account_id=acct.id, category_id=cat.id,
        description="Rent", amount=AMOUNT, type=TransactionType.EXPENSE,
        frequency=Frequency.MONTHLY, next_due_date=FIRST_DUE,
        auto_settle=True, is_active=True,
    )
    db.add(tpl)
    await db.commit()
    return {"org_id": org.id, "account_id": acct.id, "template_id": tpl.id}


async def _dates(db: AsyncSession, org_id: int) -> list[datetime.date]:
    res = await db.execute(
        select(Transaction).where(Transaction.org_id == org_id).order_by(Transaction.date)
    )
    return [t.date for t in res.scalars().all()]


async def _template(db: AsyncSession, template_id: int) -> RecurringTransaction:
    return await db.scalar(
        select(RecurringTransaction).where(RecurringTransaction.id == template_id)
    )


async def _resume(db: AsyncSession, seed: dict, *, at: datetime.date) -> None:
    """Exactly what the Resume button does.

    ``handleResume`` sends ``{is_active: true}`` and nothing else. Sending only
    that field is the point: a fix that depended on the client supplying a date
    would not run here.
    """
    await recurring_service.update_recurring(
        db, seed["org_id"], seed["template_id"],
        RecurringUpdate(is_active=True), today=at,
    )


async def _paused_across_five_months(db: AsyncSession) -> dict:
    """Generate one occurrence at T0, stop, then resume at T1 five months later."""
    seed = await _seed(db)
    await recurring_service.generate_due_transactions(db, seed["org_id"], today=T0)
    await recurring_service.stop_recurring(db, seed["org_id"], seed["template_id"])
    await _resume(db, seed, at=T1)
    return seed


async def test_resume_does_not_backfill_the_paused_gap(db_session):
    """FENCE — the headline defect.

    Wrong implementation killed: leaving ``next_due_date`` frozen across the
    ``is_active`` False->True transition (i.e. `main`). Generation then walks
    the whole paused gap and materialises Feb, Mar, Apr and May as well.
    """
    seed = await _paused_across_five_months(db_session)
    await recurring_service.generate_due_transactions(db_session, seed["org_id"], today=T1)

    dates = await _dates(db_session, seed["org_id"])
    assert dates == [FIRST_DUE, datetime.date(2026, 6, 5)], (
        f"expected only the pre-pause occurrence and the current cycle's, got {dates}"
    )


async def test_resume_does_not_apply_the_paused_gap_to_the_balance(db_session):
    """FENCE — the money consequence, asserted independently.

    This and the row-count fence can fail separately: a fix that suppressed the
    rows but still moved the balance, or one that left the gap rows PENDING (no
    balance effect), would pass exactly one of them.

    Wrong implementation killed: same as above. Every back-filled occurrence is
    written SETTLED (auto_settle + past date), and settled rows apply.
    """
    seed = await _paused_across_five_months(db_session)
    await recurring_service.generate_due_transactions(db_session, seed["org_id"], today=T1)

    acct = await db_session.scalar(
        select(Account).where(Account.id == seed["account_id"])
    )
    await db_session.refresh(acct)
    assert acct.balance == Decimal("-200.00"), (
        f"balance {acct.balance}: expected one pre-pause charge plus the current "
        f"cycle's, not the whole paused gap"
    )


async def test_resume_advances_the_frontier_onto_its_own_grid(db_session):
    """FENCE — the fix must preserve the series' alignment.

    Wrong implementation killed: ``next_due_date = today`` on resume. That stops
    the back-fill too, so the two fences above CANNOT distinguish it — but it
    silently re-anchors a monthly series from the 5th to the 20th, forever.
    """
    seed = await _paused_across_five_months(db_session)

    tpl = await _template(db_session, seed["template_id"])
    assert tpl.next_due_date.day == FIRST_DUE.day, (
        f"frontier landed on {tpl.next_due_date} (day {tpl.next_due_date.day}); "
        f"the series is anchored on day {FIRST_DUE.day} and resume must not "
        f"re-anchor it"
    )


async def test_resume_still_produces_the_current_cycle_occurrence(db_session):
    """FENCE — the fix must not overshoot.

    Wrong implementation killed: advancing to the first occurrence ``>= today``
    instead of ``>= p_start``. On this fixture that lands the frontier on
    2026-07-05, past the current cycle end, so the 2026-06-05 charge the user is
    genuinely due never appears. Both fences above stay green under it.
    """
    seed = await _paused_across_five_months(db_session)

    tpl = await _template(db_session, seed["template_id"])
    assert tpl.next_due_date == datetime.date(2026, 6, 5), (
        f"frontier {tpl.next_due_date} is not the current cycle's occurrence"
    )

    await recurring_service.generate_due_transactions(db_session, seed["org_id"], today=T1)
    assert datetime.date(2026, 6, 5) in await _dates(db_session, seed["org_id"]), (
        "the current cycle's charge was skipped"
    )


async def test_pause_and_resume_inside_one_cycle_leaves_the_frontier_alone(db_session):
    """GUARD — the common case must not regress.

    Pausing and resuming a template the same week is ordinary use. Its frontier
    is already ``>= p_start``, so the fix must leave it exactly where it is
    rather than advancing it a cycle and skipping a charge.

    Wrong implementation killed: advancing unconditionally, i.e. dropping the
    already-current early-out.
    """
    seed = await _seed(db_session)
    await recurring_service.generate_due_transactions(db_session, seed["org_id"], today=T1)
    frontier_before = (await _template(db_session, seed["template_id"])).next_due_date
    assert frontier_before >= P_START_AT_T1, "fixture precondition"

    await recurring_service.stop_recurring(db_session, seed["org_id"], seed["template_id"])
    await _resume(db_session, seed, at=T1)

    tpl = await _template(db_session, seed["template_id"])
    assert tpl.next_due_date == frontier_before
