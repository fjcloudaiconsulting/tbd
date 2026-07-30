"""One window for the forecast surfaces (TBD-243).

``compute_forecast`` and ``compute_account_balance_forecast`` used to bound
every query and every projection horizon with a bare calendar expression
(``p_start + 1 month - 1 day``). Both now resolve a single ``window_end`` from
``billing_service.period_spend_window_end``, falling back to that calendar
expression only on the roster tail (where the derived end is genuinely
``None``).

Design: ``specs/2026-07-30-forecast-period-window-design.md``. Every test here
is labelled ``fence`` (it fails against a named wrong implementation, recorded
in the docstring) or ``guard`` (regression net only — never counted as
coverage).

The clock is injected in every test; the services must never read the wall
clock when ``today`` is supplied. Fixtures are anchored to ``date.today() ± n``
rather than to literals (``reference_wall_clock_date_bomb_tests``).
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest_asyncio
from dateutil.relativedelta import relativedelta
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.billing import BillingPeriod
from app.models.category import CategoryType
from app.models.recurring import RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.services import (
    account_balance_forecast_service,
    forecast_service,
    recurring_service,
)
from app.services.loan_service import compute_pmt

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


def _calendar_fallback(p_start: datetime.date) -> datetime.date:
    """The pre-TBD-243 window: ``p_start + 1 month - 1 day``.

    Still the shipped answer on a roster tail, and used here to build fixtures
    whose rows sit deliberately outside it.
    """
    return p_start + relativedelta(months=1) - DAY


async def _seed(
    db: AsyncSession,
    *,
    open_start: datetime.date | None = None,
    closed_windows: tuple[tuple[datetime.date, datetime.date], ...] = (),
    cycle_day: int = 1,
    balance: Decimal = Decimal("1000.00"),
) -> dict:
    """One org, one checking account, one expense category, a period roster.

    ``open_start`` seeds the single open row (``end_date IS NULL``);
    ``closed_windows`` seeds the successor/stub rows that give
    ``period_effective_end`` something to derive from.
    """
    org = Organization(name="T", billing_cycle_day=cycle_day)
    db.add(org)
    await db.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db.add(at)
    await db.flush()
    acct = Account(
        org_id=org.id, name="Main", account_type_id=at.id,
        balance=balance, currency="EUR", is_default=True,
    )
    db.add(acct)
    await db.flush()
    cat = Category(org_id=org.id, name="Food", slug="food", type=CategoryType.EXPENSE)
    cat_transfer = Category(
        org_id=org.id, name="Transfer", slug="transfer",
        type=CategoryType.BOTH, is_system=True,
    )
    db.add_all([cat, cat_transfer])
    await db.flush()

    if open_start is not None:
        db.add(BillingPeriod(org_id=org.id, start_date=open_start))
    for start, end in closed_windows:
        db.add(BillingPeriod(org_id=org.id, start_date=start, end_date=end))
    await db.commit()

    return {
        "org_id": org.id,
        "account": acct,
        "account_id": acct.id,
        "cat_id": cat.id,
        "cat_transfer": cat_transfer.id,
        "account_type_id": at.id,
    }


def _tx(seed: dict, **overrides) -> Transaction:
    defaults = dict(
        org_id=seed["org_id"],
        account_id=seed["account_id"],
        category_id=seed["cat_id"],
        description="x",
        amount=Decimal("100.00"),
        type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED,
        settled_date=None,
    )
    defaults.update(overrides)
    return Transaction(**defaults)


def _settled(seed: dict, amount: str, on: datetime.date, **kw) -> Transaction:
    return _tx(
        seed, amount=Decimal(amount), status=TransactionStatus.SETTLED,
        date=on, settled_date=on, **kw,
    )


def _pending(seed: dict, amount: str, on: datetime.date, **kw) -> Transaction:
    return _tx(
        seed, amount=Decimal(amount), status=TransactionStatus.PENDING,
        date=on, settled_date=None, **kw,
    )


# ─────────────────────────────────────────────────────────────────────────────
# F1 — fence. Lapsed roster: the recorded sums run through today.
# ─────────────────────────────────────────────────────────────────────────────

async def test_f1_lapsed_open_period_counts_settled_through_today(db_session):
    """FENCE. Kills the pre-TBD-243 implementation itself.

    A months-stale open row's calendar fallback lands in the past, so a settled
    row dated today falls outside the reported window. With the floored spend
    window it is counted and ``period_end`` is today.

    Wrong implementation killed: the shipped ``main`` expression
    (``p_start + 1 month - 1 day``) — returns 7.00 and a period_end months ago.
    """
    today = datetime.date.today()
    p_start = today - datetime.timedelta(days=90)
    seed = await _seed(
        db_session,
        open_start=p_start,
        closed_windows=(
            (today - datetime.timedelta(days=60), today - datetime.timedelta(days=31)),
            (today - datetime.timedelta(days=30), today - DAY),
        ),
    )
    # 7.00 sits inside the old calendar fallback; 100.00 is dated today and
    # was invisible before this change.
    assert _calendar_fallback(p_start) < today
    db_session.add_all([
        _settled(seed, "7.00", p_start + datetime.timedelta(days=3)),
        _settled(seed, "100.00", today),
    ])
    await db_session.commit()

    fc = await forecast_service.compute_forecast(
        db_session, seed["org_id"], today=today
    )

    assert Decimal(fc["executed_expense"]) == Decimal("107")
    assert fc["period_end"] == today.isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# F2 — fence. The same lapsed roster on the account-balance surface.
# ─────────────────────────────────────────────────────────────────────────────

async def test_f2_lapsed_open_period_counts_pending_through_today(db_session):
    """FENCE. Same defect on ``compute_account_balance_forecast``.

    Wrong implementation killed: the shipped ``main`` expression — a PENDING
    250 dated today gives ``pending_delta`` 0.00 / ``expected`` 1000.00.
    """
    today = datetime.date.today()
    p_start = today - datetime.timedelta(days=90)
    seed = await _seed(
        db_session,
        open_start=p_start,
        closed_windows=(
            (today - datetime.timedelta(days=60), today - datetime.timedelta(days=31)),
            (today - datetime.timedelta(days=30), today - DAY),
        ),
    )
    db_session.add(_pending(seed, "250.00", today))
    await db_session.commit()

    res = await account_balance_forecast_service.compute_account_balance_forecast(
        db_session, seed["org_id"], today=today
    )

    row = {a["account_id"]: a for a in res["accounts"]}[seed["account_id"]]
    assert Decimal(row["pending_delta"]) == Decimal("-250.00")
    assert Decimal(row["expected_month_end_balance"]) == Decimal("750.00")
    assert res["period_end"] == today.isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# F3 — fence. Off-grid roster: the window stops at the successor, and the
#      category breakdown moves with the totals.
# ─────────────────────────────────────────────────────────────────────────────

async def test_f3_off_grid_window_stops_at_successor_including_categories(db_session):
    """FENCE. The overlap defect TBD-243 is named for, plus :155/:171.

    Open ``[T-20, NULL)`` with a successor starting ``T-5``: the calendar
    fallback runs ~T+9 and swallows the successor's window. The spend window
    stops at today.

    Wrong implementations killed:
      * bound left on the calendar fallback -> ``executed_expense`` 157.00
      * ``<`` used for ``<=`` on the upper bound -> 7.00 (today's 50 dropped)
      * totals rebound but the per-category queries at :155/:171 missed -> the
        totals asserts pass and only the category asserts fail
    """
    today = datetime.date.today()
    p_start = today - datetime.timedelta(days=20)
    seed = await _seed(
        db_session,
        open_start=p_start,
        closed_windows=(
            (today - datetime.timedelta(days=5), today + datetime.timedelta(days=25)),
        ),
    )
    assert _calendar_fallback(p_start) > today + datetime.timedelta(days=3)
    db_session.add_all([
        _settled(seed, "7.00", today - datetime.timedelta(days=19)),
        _settled(seed, "50.00", today),
        _settled(seed, "100.00", today + datetime.timedelta(days=3)),
        _pending(seed, "9.00", today + datetime.timedelta(days=3)),
    ])
    await db_session.commit()

    fc = await forecast_service.compute_forecast(
        db_session, seed["org_id"], today=today
    )

    assert Decimal(fc["executed_expense"]) == Decimal("57")
    assert Decimal(fc["pending_expense"]) == Decimal("0")
    assert fc["period_end"] == today.isoformat()

    # The breakdown must sum to the totals it is a breakdown OF.
    by_cat = {c["category_id"]: c for c in fc["categories"]}
    assert Decimal(by_cat[seed["cat_id"]]["executed"]) == Decimal("57")
    assert Decimal(by_cat[seed["cat_id"]]["pending"]) == Decimal("0")
    assert sum(Decimal(c["executed"]) for c in fc["categories"]) == Decimal(
        fc["executed_expense"]
    )
    assert sum(Decimal(c["pending"]) for c in fc["categories"]) == Decimal(
        fc["pending_expense"]
    )


# ─────────────────────────────────────────────────────────────────────────────
# F4 — fence. THE CONSERVATION FENCE.
# ─────────────────────────────────────────────────────────────────────────────

async def test_f4_forecast_net_conserved_across_generate_on_late_successor(db_session):
    """FENCE — the conservation fence. Off-grid roster with a LATE successor.

    ``generate_due_transactions`` materialises on ``current_cycle_window``,
    which is roster-independent. With ONE window the obligation is either in
    ``(today, W]`` before and ``[start, W]`` after (conserved), or outside both.
    A SPLIT (``horizon = min(derived, fallback)`` for the projection,
    ``window = max(derived, today)`` for the sums) opens a gap
    ``(horizon, window]`` that the materialisation window reaches into: the
    template is in neither bucket before and in one bucket after, and
    ``forecast_net`` moves with no user action.

    Wrong implementation killed: **the split design** — net 0 -> -100.
    Also red against ``main``, where the template is conserved at ZERO and the
    two anti-vacuity asserts (projected before / materialised after) fail.
    """
    today = datetime.date.today()
    # Cycle day <= 28 (BillingCycleUpdate's own bound) anchored on today, so
    # the materialisation window [cs, ce] always extends ~4 weeks past today
    # and contains the template's due date.
    cycle_day = min(today.day, 28)
    p_start = today - datetime.timedelta(days=40)      # off-grid open row
    successor_start = today + datetime.timedelta(days=20)   # derived end = T+19
    due = today + datetime.timedelta(days=10)

    seed = await _seed(
        db_session,
        open_start=p_start,
        closed_windows=((successor_start, successor_start + datetime.timedelta(days=29)),),
        cycle_day=cycle_day,
    )
    # The gap the split design would open must be non-empty and must contain
    # the due date, or this fence is vacuous.
    fallback = _calendar_fallback(p_start)
    derived_end = successor_start - DAY
    assert fallback < due <= derived_end

    db_session.add(RecurringTransaction(
        org_id=seed["org_id"], account_id=seed["account_id"],
        category_id=seed["cat_id"], description="rent",
        amount=Decimal("100.00"), type="expense", frequency="monthly",
        next_due_date=due, auto_settle=False, is_active=True,
    ))
    await db_session.commit()

    before = await forecast_service.compute_forecast(
        db_session, seed["org_id"], today=today
    )
    await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=today
    )
    after = await forecast_service.compute_forecast(
        db_session, seed["org_id"], today=today
    )

    # Anti-vacuity: the 100 must be PROJECTED before and MATERIALISED after.
    # Without these two the assertion below passes on a fixture where nothing
    # ever entered either bucket.
    assert Decimal(before["recurring_expense"]) == Decimal("100")
    assert Decimal(before["pending_expense"]) == Decimal("0")
    assert Decimal(after["pending_expense"]) == Decimal("100")
    assert Decimal(after["recurring_expense"]) == Decimal("0")

    assert Decimal(after["forecast_net"]) == Decimal(before["forecast_net"])


# ─────────────────────────────────────────────────────────────────────────────
# F6 — fence. A closed period is never floored.
# ─────────────────────────────────────────────────────────────────────────────

async def test_f6_closed_period_end_is_never_floored_at_today(db_session):
    """FENCE. The single most plausible refactoring slip.

    Wrong implementation killed: the floor hoisted ABOVE the
    ``period.end_date is not None`` check — it would re-open reported history
    for every org (here: today's 100 leaking into a period that closed 60 days
    ago, 40.00 -> 140.00).
    """
    today = datetime.date.today()
    p_start = today - datetime.timedelta(days=90)
    p_end = today - datetime.timedelta(days=60)
    seed = await _seed(db_session, open_start=None, closed_windows=((p_start, p_end),))
    db_session.add_all([
        _settled(seed, "40.00", today - datetime.timedelta(days=80)),
        _settled(seed, "100.00", today),
    ])
    await db_session.commit()

    fc = await forecast_service.compute_forecast(
        db_session, seed["org_id"], period_start=p_start, today=today
    )

    assert Decimal(fc["executed_expense"]) == Decimal("40")
    assert fc["period_end"] == p_end.isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# F7a / F7b — fence pair. The floor boundary, pinned from BOTH sides.
# ─────────────────────────────────────────────────────────────────────────────

async def _seed_floor_boundary(db_session, derived_end: datetime.date) -> dict:
    """Open row whose derived end is exactly ``derived_end``, plus one settled
    row on each side of it."""
    seed = await _seed(
        db_session,
        open_start=derived_end - datetime.timedelta(days=30),
        closed_windows=((derived_end + DAY, derived_end + datetime.timedelta(days=30)),),
    )
    db_session.add_all([
        _settled(seed, "5.00", derived_end - DAY),
        _settled(seed, "60.00", derived_end + DAY),
    ])
    await db_session.commit()
    return seed


async def test_f7a_floor_does_not_fire_when_today_equals_derived_end(db_session):
    """FENCE (pair a). ``today == derived`` -> the window is the derived end,
    NOT one day wider.

    Wrong implementation killed: any off-by-one widening of the floor
    (``max(derived, today) + 1 day``, or ``<`` written for ``<=`` in the wrong
    direction) — the 60.00 dated ``derived + 1`` leaks in.
    """
    derived_end = datetime.date.today() + datetime.timedelta(days=10)
    seed = await _seed_floor_boundary(db_session, derived_end)

    fc = await forecast_service.compute_forecast(
        db_session, seed["org_id"], today=derived_end
    )

    assert Decimal(fc["executed_expense"]) == Decimal("5")
    assert fc["period_end"] == derived_end.isoformat()


async def test_f7b_floor_fires_when_today_is_one_day_past_derived_end(db_session):
    """FENCE (pair b). ``today == derived + 1 day`` -> the floor DOES fire.

    Wrong implementation killed: the floor dropped entirely (calling
    ``period_effective_end`` instead of ``period_spend_window_end``) — the
    60.00 dated today is excluded and F1's whole deliverable silently reverts.
    A boundary pinned from one side is not pinned.
    """
    derived_end = datetime.date.today() + datetime.timedelta(days=10)
    seed = await _seed_floor_boundary(db_session, derived_end)
    today = derived_end + DAY

    fc = await forecast_service.compute_forecast(
        db_session, seed["org_id"], today=today
    )

    assert Decimal(fc["executed_expense"]) == Decimal("65")
    assert fc["period_end"] == today.isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# F8 — GUARD ONLY. The healthy on-grid fleet does not move.
# ─────────────────────────────────────────────────────────────────────────────

async def test_f8_guard_healthy_on_grid_period_is_unchanged(db_session):
    """GUARD, not a fence — it passes under almost every implementation and
    must never be counted as coverage.

    Named fields only. A full-payload dict comparison across two clock reads is
    the over-specification defect (``reference_over_specified_test_false_red``).
    """
    p_start = datetime.date.today()
    calendar_end = _calendar_fallback(p_start)
    seed = await _seed(
        db_session,
        open_start=p_start,
        # On-grid: the successor starts the day after the calendar end, so the
        # derived end and the calendar fallback coincide.
        closed_windows=((calendar_end + DAY, calendar_end + datetime.timedelta(days=30)),),
    )
    db_session.add_all([
        _settled(seed, "100.00", p_start + datetime.timedelta(days=2)),
        _pending(seed, "50.00", p_start + datetime.timedelta(days=3)),
    ])
    db_session.add(RecurringTransaction(
        org_id=seed["org_id"], account_id=seed["account_id"],
        category_id=seed["cat_id"], description="sub",
        amount=Decimal("20.00"), type="expense", frequency="monthly",
        next_due_date=p_start + datetime.timedelta(days=5),
        auto_settle=False, is_active=True,
    ))
    await db_session.commit()

    at_start = await forecast_service.compute_forecast(
        db_session, seed["org_id"], today=p_start
    )
    at_end = await forecast_service.compute_forecast(
        db_session, seed["org_id"], today=calendar_end
    )

    for fc in (at_start, at_end):
        assert fc["period_start"] == p_start.isoformat()
        assert fc["period_end"] == calendar_end.isoformat()
        assert Decimal(fc["executed_expense"]) == Decimal("100")
        assert Decimal(fc["pending_expense"]) == Decimal("50")
        assert Decimal(fc["executed_income"]) == Decimal("0")

    # The recurring bucket is clock-driven by design (`next_due > today`), not
    # window-driven: at p_start the instance is still upcoming, at the calendar
    # end it is not. Both values are what `main` produces for the same clock.
    assert Decimal(at_start["recurring_expense"]) == Decimal("20")
    assert Decimal(at_end["recurring_expense"]) == Decimal("0")


# ─────────────────────────────────────────────────────────────────────────────
# F9 — GUARD ONLY. The announced phantom-projection residual, pinned.
# ─────────────────────────────────────────────────────────────────────────────

async def test_f9_guard_lapsed_untracked_loan_projects_at_most_one_payment(db_session):
    """GUARD, not a fence. Pins the residual announced in the design §5.

    On a lapsed roster the window widens to ``[p_start, today]`` — months long
    — and ``due_loan_payment_dates`` has no ``> today`` gate, so an UNTRACKED
    loan (no recorded payment leg) still projects a past-dated instalment.
    That phantom is accepted, not fixed here: a past-due but genuinely unpaid
    instalment must still be projected.

    What this guard pins is that the widened window emits **one** projected
    payment, not one per scheduled date it now spans. Note: the design table
    words this row as "no past-dated phantom loan payments emitted"; that is
    not what the code does, before or after this change, and §5 of the same
    document says so explicitly. The observable behaviour is pinned here.
    """
    today = datetime.date.today()
    p_start = today - datetime.timedelta(days=90)
    seed = await _seed(
        db_session,
        open_start=p_start,
        closed_windows=(
            (today - datetime.timedelta(days=60), today - datetime.timedelta(days=31)),
            (today - datetime.timedelta(days=30), today - DAY),
        ),
    )
    loan_type = AccountType(
        org_id=seed["org_id"], name="Loan", slug="loan", is_system=True
    )
    db_session.add(loan_type)
    await db_session.flush()
    principal, apr, term = Decimal("12000.00"), Decimal("6.00"), 60
    first_payment = today - datetime.timedelta(days=70)
    loan = Account(
        org_id=seed["org_id"], name="Car Loan", account_type_id=loan_type.id,
        balance=Decimal("-10000.00"), opening_balance=Decimal("-10000.00"),
        currency="EUR", is_default=False,
        payment_source_account_id=seed["account_id"],
        principal_amount=principal, interest_rate_apr=apr, term_months=term,
        origination_date=p_start, first_payment_date=first_payment,
    )
    db_session.add(loan)
    await db_session.commit()

    res = await account_balance_forecast_service.compute_account_balance_forecast(
        db_session, seed["org_id"], today=today
    )

    by_id = {a["account_id"]: a for a in res["accounts"]}
    payments = by_id[loan.id]["loan_payments"]
    assert len(payments) == 1
    assert payments[0]["amount"] == str(compute_pmt(principal, apr, term))
    # The earliest scheduled date in the widened window — past-dated, and
    # accepted as such.
    assert payments[0]["date"] == first_payment.isoformat()
    assert p_start <= first_payment <= today


# ─────────────────────────────────────────────────────────────────────────────
# F10a / F10b — fence pair. ONE injected clock, consumed everywhere.
# ─────────────────────────────────────────────────────────────────────────────

async def test_f10a_injected_clock_bounds_the_settled_window(db_session):
    """FENCE (pair a). ``today`` is injected 40 days in the past; the window
    must floor at THAT date, not at the wall clock.

    Wrong implementation killed: dropping ``today=today`` on the
    ``period_spend_window_end`` call — the window then floors at the real wall
    clock (T+40) and the 500.00 dated ``T+1`` is counted (103 -> 603).
    """
    today = datetime.date.today() - datetime.timedelta(days=40)
    p_start = today - datetime.timedelta(days=90)
    seed = await _seed(
        db_session,
        open_start=p_start,
        closed_windows=(
            (today - datetime.timedelta(days=60), today - datetime.timedelta(days=31)),
            (today - datetime.timedelta(days=30), today - DAY),
        ),
    )
    db_session.add_all([
        _settled(seed, "3.00", today - datetime.timedelta(days=80)),
        _settled(seed, "100.00", today),
        _settled(seed, "500.00", today + DAY),
    ])
    await db_session.commit()

    fc = await forecast_service.compute_forecast(
        db_session, seed["org_id"], today=today
    )

    assert Decimal(fc["executed_expense"]) == Decimal("103")
    assert fc["period_end"] == today.isoformat()


async def test_f10b_recurring_gate_consumes_the_same_injected_clock(db_session):
    """FENCE (pair b). The recurring gate reads the SAME resolved value.

    Wrong implementation killed: leaving a bare ``datetime.date.today()`` where
    the recurring query is built (the two-clocks straddle) — the template due
    ``T+5`` fails ``next_due_date > today`` against the real wall clock (T+40)
    and the whole recurring contribution silently disappears.
    """
    today = datetime.date.today() - datetime.timedelta(days=40)
    p_start = today - datetime.timedelta(days=20)
    successor_start = today + datetime.timedelta(days=10)
    seed = await _seed(
        db_session,
        open_start=p_start,
        closed_windows=((successor_start, successor_start + datetime.timedelta(days=29)),),
    )
    db_session.add(RecurringTransaction(
        org_id=seed["org_id"], account_id=seed["account_id"],
        category_id=seed["cat_id"], description="sub",
        amount=Decimal("100.00"), type="expense", frequency="monthly",
        next_due_date=today + datetime.timedelta(days=5),
        auto_settle=False, is_active=True,
    ))
    await db_session.commit()

    fc = await forecast_service.compute_forecast(
        db_session, seed["org_id"], today=today
    )

    assert Decimal(fc["recurring_expense"]) == Decimal("100")
    assert fc["period_end"] == (successor_start - DAY).isoformat()
