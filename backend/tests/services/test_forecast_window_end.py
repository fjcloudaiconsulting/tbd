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
from app.models.account import PaymentStrategy
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
from app.services.billing_service import current_cycle_window
from app.services.loan_forecast_service import due_loan_payment_dates
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
    cat_income = Category(
        org_id=org.id, name="Salary", slug="salary", type=CategoryType.INCOME
    )
    cat_transfer = Category(
        org_id=org.id, name="Transfer", slug="transfer",
        type=CategoryType.BOTH, is_system=True,
    )
    db.add_all([cat, cat_income, cat_transfer])
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
        "cat_income": cat_income.id,
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

    ⚠ **Scope of the conservation claim (widened by TBD-260).** Conservation is
    now GENERAL, not narrow. This fence pins the FUTURE-dated case
    (``next_due_date > today``), which is what the one-window decision buys;
    ``test_g2_overdue_template_conserves_forecast_net_on_both_rosters`` pins the
    OVERDUE case (``next_due_date <= today``), which the occurrence bound buys.
    Together they cover the whole grid: ``recurring_*`` projects exactly the
    un-materialised occurrences in ``[p_start, window_end]`` and
    ``pending_*``/``executed_*`` count exactly the materialised ones, so
    ``generate_due_transactions`` can only move an amount BETWEEN buckets.

    The two are still separate fences. This one is the only one that catches the
    split design, whose extra break is on a FUTURE-dated template and which G2's
    fixture cannot see.
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
# F8 — fence (was a guard). The healthy on-grid payload is CLOCK-INDEPENDENT.
# ─────────────────────────────────────────────────────────────────────────────

async def test_f8_guard_healthy_on_grid_period_is_unchanged(db_session):
    """FENCE (promoted from guard by TBD-260).

    The named-field asserts below are the original guard: the healthy on-grid
    fleet's settled/pending buckets do not move. Those alone pass under almost
    every implementation and are not coverage.

    The FENCE is the field-by-field equality of the two payloads. TBD-260
    removed the last clock predicate from the recurring path (the query is now
    bound on ``window_end`` alone), so on a roster where the ``max(derived,
    today)`` floor is demonstrably inert, ``compute_forecast`` is a pure
    function of the data — the same call with ``today`` at the period start and
    at the period end must return the SAME payload, byte for byte.

    Wrong implementations killed:
      * any residual ``today`` predicate in the recurring selection or the
        occurrence walk — structurally, not merely by value. The shipped
        ``next_due_date > today`` gate makes ``at_end["recurring_expense"]``
        0 while ``at_start``'s is 20;
      * the ticket's proposed ``next_due_date >= p_start`` bound is NOT killed
        here (it is clock-free too) — F13 in
        ``test_forecast_overdue_recurring.py`` is its fence.

    ⚠ This is a payload equality across two values of ONE INJECTED clock, not
    across two wall-clock reads. The over-specification defect
    (``reference_over_specified_test_false_red``) is a full-payload comparison
    whose two sides are allowed to differ; here they are required to be
    identical, and that requirement IS the deliverable.
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

    # The recurring bucket is window-driven, and clock-independent except
    # through `window_end`. The instance due `p_start + 5` is an occurrence of
    # this period whether it is asked about on the first day of the period or
    # the last: it has not been materialised either way, and the obligation
    # does not stop existing because the calendar moved past it.
    assert Decimal(at_start["recurring_expense"]) == Decimal("20")
    assert Decimal(at_end["recurring_expense"]) == Decimal("20")

    # Fixture precondition for the equality below: the derived end and the
    # calendar fallback coincide, and BOTH injected clock values sit at or
    # before it — so `max(derived, today)` is demonstrably INERT and any
    # difference between the two payloads is the recurring path reading the
    # clock, not the window floor moving under the test.
    assert p_start <= calendar_end
    assert at_start["period_end"] == at_end["period_end"] == calendar_end.isoformat()

    # THE FENCE. Every named field, plus the breakdown.
    named = [k for k in at_start if k != "categories"]
    assert len(named) == 12, named   # the payload is not silently empty
    for key in named:
        assert at_start[key] == at_end[key], key
    assert at_start["categories"] == at_end["categories"]


# ─────────────────────────────────────────────────────────────────────────────
# F9 — fence. The LOAN SYNTHESIS HORIZON is bound to the window, and the
#      announced phantom-projection residual is pinned with it.
# ─────────────────────────────────────────────────────────────────────────────

_LOAN_PRINCIPAL = Decimal("12000.00")
_LOAN_APR = Decimal("6.00")
_LOAN_TERM = 60


async def _seed_lapsed_loan(
    db_session, *, today: datetime.date, first_payment: datetime.date
) -> tuple[dict, Account]:
    """Lapsed roster (open ``[T-90, NULL)``, historic stubs) + an untracked
    loan whose first scheduled payment is ``first_payment``."""
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
    loan = Account(
        org_id=seed["org_id"], name="Car Loan", account_type_id=loan_type.id,
        balance=Decimal("-10000.00"), opening_balance=Decimal("-10000.00"),
        currency="EUR", is_default=False,
        payment_source_account_id=seed["account_id"],
        principal_amount=_LOAN_PRINCIPAL, interest_rate_apr=_LOAN_APR,
        term_months=_LOAN_TERM,
        origination_date=p_start, first_payment_date=first_payment,
    )
    db_session.add(loan)
    await db_session.commit()
    seed["p_start"] = p_start
    return seed, loan


async def test_f9_lapsed_untracked_loan_projects_from_the_widened_window(db_session):
    """FENCE. The loan synthesis horizon (``p_end=window_end``), plus the
    phantom-projection residual announced in the design §5.

    The first scheduled payment is placed at ``T-40`` — deliberately PAST the
    old calendar fallback (``p_start + 1 month - 1 day`` ~ ``T-60``) and inside
    the widened window ``[T-90, T]``. The window therefore spans TWO scheduled
    dates (``T-40`` and ``T-40 + 1 month``) while the synthesizer projects only
    the earliest.

    Wrong implementations killed:
      * the loan synthesizer left on the calendar fallback
        (``p_end=p_start + 1 month - 1 day``) -> no scheduled date in-window ->
        ``loan_payments == []`` and the source keeps its full balance;
      * a synthesizer that stopped emitting the EARLIEST in-window date;
      * a widened window that emitted one payment PER scheduled date.

    ⚠ **Why the previous assertion here was worthless.**
    ``synthesize_account_loan_payment`` returns ``dates[0]`` — a list of 0 or 1
    elements BY CONSTRUCTION — so the old ``len(payments) == 1`` could never
    detect the multiplicity it claimed to pin, and with the old fixture
    (``first_payment = T-70``, inside the calendar fallback) it stayed green
    with the horizon left on the fallback too. It was the eighteenth instance of
    this repo's signature defect. The assertions below are stated over values
    that actually move: the emitted payment list, and the number of scheduled
    dates the window spans.
    """
    today = datetime.date.today()
    first_payment = today - datetime.timedelta(days=40)
    seed, loan = await _seed_lapsed_loan(
        db_session, today=today, first_payment=first_payment
    )
    p_start = seed["p_start"]
    # Fixture preconditions — without these the fence is decoration.
    assert _calendar_fallback(p_start) < first_payment <= today
    # The widened window spans MORE than one scheduled date; the synthesizer
    # collapses them to one. That collapse is the residual, not a fix.
    spanned = due_loan_payment_dates(first_payment, _LOAN_TERM, p_start, today)
    assert len(spanned) == 2
    assert spanned[0] == first_payment

    res = await account_balance_forecast_service.compute_account_balance_forecast(
        db_session, seed["org_id"], today=today
    )

    by_id = {a["account_id"]: a for a in res["accounts"]}
    pmt = compute_pmt(_LOAN_PRINCIPAL, _LOAN_APR, _LOAN_TERM)
    # Exactly one projected payment, on the EARLIEST in-window date — which is
    # past-dated, and accepted as such (§5: a past-due but genuinely unpaid
    # instalment must still be projected; do NOT add a `> today` gate).
    assert by_id[loan.id]["loan_payments"] == [
        {"amount": str(pmt), "date": first_payment.isoformat()}
    ]
    # And the projection really did move money, so the assertion above is not
    # passing on an inert payload.
    source_row = by_id[seed["account_id"]]
    assert Decimal(source_row["expected_month_end_balance"]) == (
        Decimal(source_row["balance"]) - pmt
    )


# ─────────────────────────────────────────────────────────────────────────────
# F11 — fence. The loan ``already_paid`` probe reads the WINDOW, inclusively.
# ─────────────────────────────────────────────────────────────────────────────

async def test_f11_loan_already_paid_probe_uses_the_window(db_session):
    """FENCE. The anti-double-count probe at the loan site.

    A recorded payment-in leg dated exactly on ``window_end`` (== today on a
    lapsed roster) must suppress the projection. Two independent wrong
    implementations are killed:

      * the probe left on the old calendar fallback
        (``eff_date <= p_start + 1 month - 1 day``) — the leg is months past
        that bound, ``already_paid`` stays False, and the loan is projected a
        SECOND time on top of the leg the user already recorded;
      * ``<`` written for ``<=`` on the probe's upper bound — the leg dated
        exactly on the boundary is dropped, same phantom.

    The leg is deliberately placed ON the boundary: a boundary pinned from one
    side is not pinned.
    """
    today = datetime.date.today()
    first_payment = today - datetime.timedelta(days=40)
    seed, loan = await _seed_lapsed_loan(
        db_session, today=today, first_payment=first_payment
    )
    # The recorded payment: a reciprocal transfer pair, INCOME on the loan.
    loan_leg = _tx(
        seed, account_id=loan.id, category_id=seed["cat_transfer"],
        amount=Decimal("232.00"), type=TransactionType.INCOME,
        status=TransactionStatus.SETTLED, date=today, settled_date=today,
    )
    source_leg = _tx(
        seed, category_id=seed["cat_transfer"],
        amount=Decimal("232.00"), type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED, date=today, settled_date=today,
    )
    db_session.add_all([loan_leg, source_leg])
    await db_session.flush()
    loan_leg.linked_transaction_id = source_leg.id
    source_leg.linked_transaction_id = loan_leg.id
    await db_session.commit()

    res = await account_balance_forecast_service.compute_account_balance_forecast(
        db_session, seed["org_id"], today=today
    )

    by_id = {a["account_id"]: a for a in res["accounts"]}
    # Fixture precondition: the leg sits exactly on the reported window end.
    assert res["period_end"] == today.isoformat()
    assert by_id[loan.id]["loan_payments"] == []
    source_row = by_id[seed["account_id"]]
    assert Decimal(source_row["expected_month_end_balance"]) == Decimal(
        source_row["balance"]
    )


# ─────────────────────────────────────────────────────────────────────────────
# F12 — fence. ``window_end`` is the INCLUSIVE upper bound of every bucket.
# ─────────────────────────────────────────────────────────────────────────────

async def test_f12_window_end_is_inclusive_for_every_bucket(db_session):
    """FENCE. Pins nine upper bounds from the TOP and the recurring LOWER bound
    from both sides, in one fixture.

    A systematic sweep that flips ``<= window_end`` to ``< window_end`` one
    site at a time left EIGHT of the eleven bounds green before this test:
    ``forecast_service`` settled-income, pending-income, pending-expense, the
    recurring query gate, the recurring projection loop, and the three
    per-category equivalents. F3's pending assertions are ``0 == 0`` and cannot
    detect any of it. This repo's rule is that a boundary pinned from one side
    is not pinned.

    Roster: off-grid with a LATE successor, so ``window_end`` is the derived
    end ``T+19`` (in the FUTURE). Every bucket gets one row ON ``window_end``
    and one row on ``window_end + 1``.

    ⚠ Until TBD-260 this test pinned NINE upper bounds and ZERO lower bounds.
    Two YEARLY templates now pin the recurring lower bound from both sides: one
    whose only occurrence lands exactly ON ``p_start`` (must count) and one
    whose grid genuinely misses the window (must not).

    YEARLY, not monthly, for BOTH of them, and deliberately:
      * a monthly template at ``p_start`` on this 60-day-wide fixture has a
        second occurrence whose position relative to ``window_end`` swings with
        month length — a date bomb;
      * a monthly template at ``p_start - 1 day`` has an occurrence a month
        later that lands INSIDE the window, so the right answer and the wrong
        answer agree and the fence is vacuous.

    Wrong implementations killed:
      * ``<`` for ``<=`` at any of ``forecast_service.py`` settled-income /
        settled-expense / pending-income / pending-expense / the recurring
        query gate / the occurrence walk's upper bound / category-executed /
        category-pending / category-recurring;
      * ``while d <= start`` for ``while d < start`` in
        ``occurrences_in_window``'s fast-forward — the 29.00 ON ``p_start`` is
        skipped and its next occurrence is a year out (52 -> 23);
      * no lower bound at all on the occurrence walk — the 997.00 template
        dated ``p_start - 1`` leaks in (52 -> 1049).
    """
    today = datetime.date.today()
    p_start = today - datetime.timedelta(days=40)
    successor_start = today + datetime.timedelta(days=20)
    window_end = successor_start - DAY
    over = successor_start                      # window_end + 1 day
    seed = await _seed(
        db_session,
        open_start=p_start,
        closed_windows=((successor_start, successor_start + datetime.timedelta(days=29)),),
    )
    # The derived end must be in the FUTURE, or the floor moves the boundary
    # under the test and the recurring bounds become unreachable.
    assert window_end > today
    inc = seed["cat_income"]
    db_session.add_all([
        _settled(seed, "11.00", window_end, type=TransactionType.INCOME, category_id=inc),
        _settled(seed, "500.00", over, type=TransactionType.INCOME, category_id=inc),
        _settled(seed, "13.00", window_end),
        _settled(seed, "500.00", over),
        _pending(seed, "17.00", window_end, type=TransactionType.INCOME, category_id=inc),
        _pending(seed, "500.00", over, type=TransactionType.INCOME, category_id=inc),
        _pending(seed, "19.00", window_end),
        _pending(seed, "500.00", over),
    ])
    db_session.add_all([
        # ON the upper bound.
        RecurringTransaction(
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=seed["cat_id"], description="rent",
            amount=Decimal("23.00"), type="expense", frequency="monthly",
            next_due_date=window_end, auto_settle=False, is_active=True,
        ),
        # ON the lower bound — its only occurrence in a year is p_start.
        RecurringTransaction(
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=seed["cat_id"], description="insurance",
            amount=Decimal("29.00"), type="expense", frequency="yearly",
            next_due_date=p_start, auto_settle=False, is_active=True,
        ),
        # One day BELOW the lower bound, and its grid genuinely misses the
        # window: the next occurrence is `p_start - 1 + 1 year`.
        RecurringTransaction(
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=seed["cat_id"], description="tax",
            amount=Decimal("997.00"), type="expense", frequency="yearly",
            next_due_date=p_start - DAY, auto_settle=False, is_active=True,
        ),
    ])
    await db_session.commit()

    fc = await forecast_service.compute_forecast(
        db_session, seed["org_id"], today=today
    )

    assert fc["period_end"] == window_end.isoformat()
    assert Decimal(fc["executed_income"]) == Decimal("11")
    assert Decimal(fc["executed_expense"]) == Decimal("13")
    assert Decimal(fc["pending_income"]) == Decimal("17")
    assert Decimal(fc["pending_expense"]) == Decimal("19")
    # 23 on window_end + 29 on p_start; the 997 one day below is excluded.
    assert Decimal(fc["recurring_expense"]) == Decimal("52")

    by_cat = {c["category_id"]: c for c in fc["categories"]}
    row = by_cat.get(seed["cat_id"], {"executed": "0", "pending": "0", "recurring": "0"})
    assert Decimal(row["executed"]) == Decimal("13")
    assert Decimal(row["pending"]) == Decimal("19")
    assert Decimal(row["recurring"]) == Decimal("52")


# ─────────────────────────────────────────────────────────────────────────────
# G1 — GUARD (and the fence for the CC synthesis horizon). The CC phantom
#      payments announced in §5 MULTIPLY per cycle on a lapsed roster.
# ─────────────────────────────────────────────────────────────────────────────

async def test_g1_cc_phantom_payments_multiply_per_cycle_on_lapsed_roster(db_session):
    """GUARD — pins the ACTUAL behaviour, which is not an aspiration.
    Doubles as the FENCE for the CC synthesis horizon (``p_end=window_end``).

    On a lapsed roster the window widens to ``[p_start, today]``, months long,
    and ``due_cycles_in_horizon`` has no ``> today`` gate, so EVERY cycle whose
    ``payment_date`` falls in that span is projected. This is the same residual
    §5 announces for loans, but it multiplies: one phantom PER CYCLE rather than
    one per period. ``main`` projected none (its window ended before the first
    payment date). ``CreditUtilizationWidget.tsx`` renders "Next payment ... on
    <date>" from this list, so the user sees a past date.

    The multiplication is BOUNDED by the outstanding balance: ``s_prev`` threads
    each synthesized outflow forward, so the payments sum to the balance owed at
    the last projected close, never to a multiple of it. That bound is what
    makes this acceptable-and-announced rather than a defect. Do NOT add a
    ``> today`` gate here — §5 explains that it would delete genuinely unpaid
    past-due obligations.

    Wrong implementation killed: the CC synthesizer left on the old calendar
    fallback (``p_end=p_start + 1 month - 1 day``) -> ``cc_payments == []`` and
    the source keeps its full 5000.00.
    """
    today = datetime.date.today()
    p_start = today - relativedelta(months=3)
    seed = await _seed(
        db_session,
        open_start=p_start,
        closed_windows=(
            (today - datetime.timedelta(days=60), today - datetime.timedelta(days=31)),
            (today - datetime.timedelta(days=30), today - DAY),
        ),
        balance=Decimal("5000.00"),
    )
    cc_type = AccountType(
        org_id=seed["org_id"], name="Credit Card", slug="credit_card", is_system=True
    )
    db_session.add(cc_type)
    await db_session.flush()
    cc = Account(
        org_id=seed["org_id"], name="Visa", account_type_id=cc_type.id,
        balance=Decimal("-900.00"), opening_balance=Decimal("0.00"),
        currency="EUR", is_default=False,
        close_day=10, payment_day=5, payment_day_relative_month=1,
        payment_source_account_id=seed["account_id"],
        payment_strategy=PaymentStrategy.FULL_BALANCE,
    )
    db_session.add(cc)
    await db_session.flush()
    # Three 300.00 charges, one per month of the lapsed span, no payment legs.
    for k in range(3):
        on = p_start + datetime.timedelta(days=5) + relativedelta(months=k)
        db_session.add(_settled(seed, "300.00", on, account_id=cc.id))
    await db_session.commit()

    res = await account_balance_forecast_service.compute_account_balance_forecast(
        db_session, seed["org_id"], today=today
    )

    by_id = {a["account_id"]: a for a in res["accounts"]}
    payments = by_id[cc.id]["cc_payments"]
    # MORE THAN ONE phantom, and every one of them past-dated.
    assert len(payments) == 2
    assert all(
        datetime.date.fromisoformat(p["date"]) < today for p in payments
    )
    assert [p["date"] for p in payments] == sorted(p["date"] for p in payments)
    # Bounded by the outstanding balance at the last projected close, NOT a
    # multiple of it: two 300.00 cycles against 600.00 of charges closed.
    assert sum(Decimal(p["amount"]) for p in payments) == Decimal("600.00")
    source_row = by_id[seed["account_id"]]
    assert Decimal(source_row["balance"]) == Decimal("5000.00")
    assert Decimal(source_row["expected_month_end_balance"]) == Decimal("4400.00")
    assert Decimal(by_id[cc.id]["expected_month_end_balance"]) == Decimal("-300.00")


# ─────────────────────────────────────────────────────────────────────────────
# G2 — fence (was a guard). An OVERDUE template conserves on BOTH rosters.
# ─────────────────────────────────────────────────────────────────────────────

async def test_g2_overdue_template_conserves_forecast_net_on_both_rosters(db_session):
    """FENCE (promoted from guard by TBD-260). The third case F4 does not reach.

    F4 fences conservation for a template due in the FUTURE. This one fences the
    case the TBD-243 design explicitly declined to fix and handed off:
    ``next_due_date <= today``, i.e. an OVERDUE template.

    ``generate_due_transactions`` materialises an overdue template regardless of
    the roster — its catch-up loop has no lower bound and its window is
    ``current_cycle_window``, which is roster-independent. So the obligation
    must ALREADY be in ``recurring_*`` before generation runs, or the scheduler
    moves ``forecast_net`` every 900 seconds with no user action. It is, now:
    the recurring path bounds the OCCURRENCE by ``[p_start, window_end]`` and
    carries no clock predicate at all.

    Wrong implementations killed:
      * the shipped ``next_due_date > today`` gate — the template is in NEITHER
        bucket before and in ``pending_*`` after: ``(0, -100.00)``;
      * the ticket's proposed ``next_due_date >= p_start`` — same numbers here
        (``next_due`` is ``T-3``, which IS ``>= p_start`` on both rosters, so
        this fixture does not discriminate it; F13 in
        ``test_forecast_overdue_recurring.py`` does);
      * no probe at all — ``(−100.00, −200.00)`` after, the occurrence counted
        in ``recurring_*`` and again in ``pending_*``.

    ⚠ **Assert the VALUE, never just ``before == after``.** On ``main`` the
    lapsed arm returns ``(0, 0)`` — it conserves by not looking, because its
    stale window drops the materialised row entirely. A pure ``before == after``
    assertion is green on that, which is why the tuple below is pinned to
    ``(−100.00, −100.00)`` on both arms.

    ⚠ **Both roster preconditions are asserted explicitly.** The forecast
    window (``window_end``) and the materialisation window
    (``current_cycle_window``) COINCIDE on the on-grid arm and DIVERGE on the
    lapsed arm. A design that conserved only when the two coincide passes the
    on-grid arm alone.
    """
    today = datetime.date.today()
    due = today - datetime.timedelta(days=3)

    async def _net_move(seed: dict) -> tuple[Decimal, Decimal]:
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
        # Anti-vacuity: the 100 must be PROJECTED before and MATERIALISED
        # after, and it must be in EXACTLY ONE bucket on each side.
        assert Decimal(before["recurring_expense"]) == Decimal("100")
        assert Decimal(before["pending_expense"]) == Decimal("0")
        assert Decimal(after["pending_expense"]) == Decimal("100")
        assert Decimal(after["recurring_expense"]) == Decimal("0")
        return Decimal(before["forecast_net"]), Decimal(after["forecast_net"])

    # (a) healthy on-grid: the forecast window IS the materialisation window.
    on_grid_cycle_day = min((today - datetime.timedelta(days=5)).day, 28)
    cs, ce = current_cycle_window(on_grid_cycle_day, today)
    calendar_end = _calendar_fallback(cs)
    on_grid = await _seed(
        db_session,
        open_start=cs,
        closed_windows=((calendar_end + DAY, calendar_end + datetime.timedelta(days=30)),),
        cycle_day=on_grid_cycle_day,
    )
    # Precondition (a): window_end == ce > today, and the overdue occurrence
    # is inside the period.
    fc = await forecast_service.compute_forecast(db_session, on_grid["org_id"], today=today)
    assert fc["period_end"] == ce.isoformat()
    assert cs <= due < today < ce
    assert await _net_move(on_grid) == (Decimal("-100.00"), Decimal("-100.00"))

    # (b) lapsed: the forecast window stops at today, the materialisation
    #     window runs weeks past it. The two DIVERGE.
    lapsed_cycle_day = min(today.day, 28)
    _, lapsed_ce = current_cycle_window(lapsed_cycle_day, today)
    lapsed_start = today - datetime.timedelta(days=90)
    lapsed = await _seed(
        db_session,
        open_start=lapsed_start,
        closed_windows=(
            (today - datetime.timedelta(days=60), today - datetime.timedelta(days=31)),
            (today - datetime.timedelta(days=30), today - DAY),
        ),
        cycle_day=lapsed_cycle_day,
    )
    # Precondition (b): window_end == today < ce.
    fc = await forecast_service.compute_forecast(db_session, lapsed["org_id"], today=today)
    assert fc["period_end"] == today.isoformat()
    assert lapsed_start <= due < today < lapsed_ce
    assert await _net_move(lapsed) == (Decimal("-100.00"), Decimal("-100.00"))


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
    """FENCE (pair b). The recurring projection is bound to the SAME window the
    injected clock produced.

    ⚠ **Re-aimed by TBD-260.** This test used to kill "a bare
    ``datetime.date.today()`` where the recurring query is built". That wrong
    implementation is now UNEXPRESSIBLE: the recurring query references no clock
    at all, only ``window_end``. Leaving the old docstring in place would claim
    protection the code shape no longer permits — worse than deleting the test.

    What survives, and what this now kills, is the ONE remaining route by which
    a clock reaches the recurring bucket — ``window_end``:

      * a SECOND ``window_end`` computed for the recurring query (e.g. left on
        the pre-TBD-243 calendar fallback ``p_start + 1 month - 1 day``, which
        here runs to ``T-20 + 1 month ~ T+10``, past the derived end ``T+9``);
      * ``today=today`` dropped on the ``period_spend_window_end`` call at the
        top — the window then floors at the real wall clock (``T+40``), the
        reported ``period_end`` is wrong, and the successor's window is
        swallowed.

    The body is unchanged. ``period_end`` is asserted alongside the amount
    precisely because the amount alone no longer distinguishes them.
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
