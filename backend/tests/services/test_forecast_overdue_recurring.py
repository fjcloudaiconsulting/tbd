"""Overdue recurring templates are projected, not dropped (TBD-260).

``compute_forecast`` selected recurring templates with
``next_due_date <= window_end AND next_due_date > today``. An OVERDUE template
contributed zero to ``recurring_*``, but ``generate_due_transactions``
materialises it anyway — its window is ``current_cycle_window``, which is
roster-independent, and its catch-up loop has no lower bound. So the obligation
landed in ``pending_*`` having never been in ``recurring_*``, and
``forecast_expense`` moved with no user action, every 900 seconds, on the
scheduler's tick.

The fix bounds the OCCURRENCE, not ``next_due_date``. ``next_due_date`` is a
FRONTIER — the next un-materialised occurrence — so a template whose frontier
sits before ``p_start`` still has occurrences inside the window and generation
materialises all of them. The invariant now is:

    ``recurring_*`` projects exactly the occurrences of each active template
    that fall in ``[p_start, window_end]`` and have NOT already been
    materialised. ``pending_*``/``executed_*`` count exactly the materialised
    ones. The two sets partition the same occurrence grid.

Design: ``specs/2026-07-30-forecast-overdue-recurring-design.md``. Every test
here is a ``fence`` — its docstring names the wrong implementation it goes RED
against, and every one of them was injected and confirmed RED before this file
was committed.

The clock is injected in every test; fixtures are anchored to
``date.today() ± n`` rather than to literals
(``reference_wall_clock_date_bomb_tests``). F17 is the one exception and says
why.
"""
from __future__ import annotations

import ast
import datetime
import inspect
from decimal import Decimal

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
from app.models.recurring import Frequency, RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.schemas.recurring import RecurringUpdate
from app.schemas.transaction import PromoteToRecurringRequest
from app.services import forecast_service, recurring_service, transaction_service
from app.services.billing_service import current_cycle_window
from app.services.date_utils import (
    MAX_OCCURRENCE_ITERATIONS,
    advance_date,
    occurrences_in_window,
)

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
    """The on-grid period window: ``p_start + 1 month - 1 day``."""
    return p_start + relativedelta(months=1) - DAY


def _safe_month_anchor(d: datetime.date) -> datetime.date:
    """Nudge ``d`` back to a day-of-month that exists in EVERY month (<= 28).

    ``advance_date`` is path-dependent at month ends: Jan 31 -> Feb 28 -> Mar 28,
    so ``(p_start - 1 month) + 1 month == p_start`` holds only when
    ``p_start.day <= 28``. Fixtures below rely on that identity to place an
    occurrence exactly ON ``p_start``; without this anchor they would be date
    bombs that fail only in the last three days of a long month
    (``reference_wall_clock_date_bomb_tests``).

    F17 exercises the month-end path deliberately, with fixed literals. Every
    other fixture here avoids it.
    """
    while d.day > 28:
        d -= DAY
    return d


async def _seed(
    db: AsyncSession,
    *,
    open_start: datetime.date | None = None,
    closed_windows: tuple[tuple[datetime.date, datetime.date], ...] = (),
    cycle_day: int = 1,
    balance: Decimal = Decimal("1000.00"),
) -> dict:
    """One org, TWO checking accounts, an expense/income/transfer category set,
    a period roster.

    Scaffolding mirrors ``test_forecast_window_end._seed``; the second account
    exists so F18 can build a genuine transfer pair (``_link_pair`` refuses two
    legs on the same account).
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
    acct_b = Account(
        org_id=org.id, name="Savings", account_type_id=at.id,
        balance=balance, currency="EUR", is_default=False,
    )
    db.add_all([acct, acct_b])
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
        "account_b_id": acct_b.id,
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


def _template(seed: dict, **overrides) -> RecurringTransaction:
    defaults = dict(
        org_id=seed["org_id"],
        account_id=seed["account_id"],
        category_id=seed["cat_id"],
        description="rent",
        amount=Decimal("100.00"),
        type="expense",
        frequency="monthly",
        auto_settle=False,
        is_active=True,
    )
    defaults.update(overrides)
    return RecurringTransaction(**defaults)


async def _rows(db: AsyncSession, org_id: int) -> list[Transaction]:
    res = await db.execute(
        select(Transaction)
        .where(Transaction.org_id == org_id)
        .order_by(Transaction.date, Transaction.id)
    )
    return list(res.scalars().all())


async def _seed_on_grid(
    db: AsyncSession, *, today: datetime.date, days_before: int
) -> tuple[dict, datetime.date, datetime.date]:
    """A HEALTHY on-grid org whose open period started ``~days_before`` ago.

    Returns ``(seed, p_start, window_end)``. ``p_start`` is anchored to a
    day-of-month <= 28 (see ``_safe_month_anchor``) and the org's billing cycle
    day is set to match, so the forecast window and the materialisation window
    ``current_cycle_window(cycle_day, today)`` COINCIDE exactly — asserted here
    rather than assumed, because every conservation claim below is a claim about
    those two windows agreeing.
    """
    p_start = _safe_month_anchor(today - datetime.timedelta(days=days_before))
    window_end = _calendar_fallback(p_start)
    cycle_day = p_start.day
    seed = await _seed(
        db,
        open_start=p_start,
        closed_windows=((window_end + DAY, window_end + datetime.timedelta(days=30)),),
        cycle_day=cycle_day,
    )
    assert current_cycle_window(cycle_day, today) == (p_start, window_end)
    return seed, p_start, window_end


# ─────────────────────────────────────────────────────────────────────────────
# F13 — fence. The pre-``p_start`` frontier, BOTH lower-bound sides at once.
# ─────────────────────────────────────────────────────────────────────────────

async def test_f13_pre_period_frontier_projects_only_in_window_occurrences(db_session):
    """FENCE. The ticket's own prescribed fix is a NULL FIX, and this kills it.

    A monthly template whose frontier is a FULL PERIOD before ``p_start``:
    occurrences ``{p_start - 1 month, p_start, p_start + 1 month}``, of which
    exactly ONE — ``p_start`` — is inside ``[p_start, window_end]``.
    ``generate_due_transactions`` materialises the two at or below
    ``window_end``, including the one dated BEFORE ``p_start``, which belongs to
    the previous period and must never have been projected here.

    Wrong implementations killed:
      * the ticket's ``RecurringTransaction.next_due_date >= p_start`` — the
        template is excluded by its stale frontier, ``before.recurring`` is 0
        and ``forecast_net`` still moves 0 -> -100. The SAME break as ``main``,
        in the same direction, by the same amount;
      * the shipped ``next_due_date > today`` — identical numbers;
      * no lower bound at all on the occurrence walk — ``p_start - 1 month`` is
        counted too, ``before.recurring`` is 200 and the net moves -200 -> -100;
      * ``d <= start`` for ``d < start`` in ``occurrences_in_window``'s
        fast-forward — the occurrence ON ``p_start`` is skipped,
        ``before.recurring`` is 0;
      * ``d = max(next_due, p_start)`` — projects ``p_start`` and then
        ``p_start + 1 month``... on the WRONG grid for a month-end template, and
        here it happens to agree; F17 is the fence for the grid itself.

    ⚠ The frontier must be a FULL PERIOD before ``p_start``. At ``p_start - 1``
    the ticket's version and this one agree and the fence is vacuous.

    ⚠ The occurrence landing EXACTLY ON ``p_start`` pins the bound from the
    "in" side; the one a month earlier pins it from the "out" side. A boundary
    pinned from one side is not pinned.
    """
    today = datetime.date.today()
    seed, p_start, window_end = await _seed_on_grid(db_session, today=today, days_before=5)
    frontier = p_start - relativedelta(months=1)

    # Fixture preconditions — without these the fence is decoration.
    assert frontier < p_start
    assert advance_date(frontier, Frequency.MONTHLY) == p_start   # a FULL period
    assert p_start + relativedelta(months=1) > window_end          # only ONE in-window
    assert window_end > today

    db_session.add(_template(seed, next_due_date=frontier))
    await db_session.commit()

    before = await forecast_service.compute_forecast(
        db_session, seed["org_id"], today=today
    )
    # Exactly the occurrence ON p_start. Not 0 (frontier-gated), not 200.
    assert Decimal(before["recurring_expense"]) == Decimal("100")
    assert Decimal(before["pending_expense"]) == Decimal("0")

    res = await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=today
    )
    # Anti-vacuity for the lower bound: generation really does reach BELOW
    # p_start, which is exactly why the projection must not.
    assert res["generated"] == 2
    rows = await _rows(db_session, seed["org_id"])
    assert [r.date for r in rows] == [frontier, p_start]
    assert len([r for r in rows if r.date < p_start]) == 1

    after = await forecast_service.compute_forecast(
        db_session, seed["org_id"], today=today
    )
    assert Decimal(after["pending_expense"]) == Decimal("100")
    assert Decimal(after["recurring_expense"]) == Decimal("0")
    assert Decimal(before["forecast_net"]) == Decimal("-100")
    assert Decimal(after["forecast_net"]) == Decimal("-100")


# ─────────────────────────────────────────────────────────────────────────────
# F14 — fence. MULTIPLICITY. Must be WEEKLY.
# ─────────────────────────────────────────────────────────────────────────────

async def test_f14_weekly_overdue_template_projects_every_in_window_occurrence(db_session):
    """FENCE. The projection counts EVERY un-materialised in-window occurrence.

    Weekly 10.00, frontier 11 days before ``p_start``. The grid crossing the
    window is ``{p_start-11, p_start-4, p_start+3, p_start+10, p_start+17,
    p_start+24, p_start+31}``; ``window_end`` is ``p_start + 27..30``, so
    exactly FOUR occurrences are in-window (40.00) and generation materialises
    SIX (two below ``p_start``).

    ⚠ **WEEKLY is load-bearing.** A monthly template yields ONE occurrence under
    the right implementation and under every wrong one listed below, so the
    fence would be vacuous. Multiplicity is the whole content of this test.

    Wrong implementations killed:
      * counting from ``next_due`` with no fast-forward — 60.00 (the two
        pre-period occurrences counted);
      * counting only the FIRST in-window occurrence, or ``d = max(next_due,
        p_start)`` collapsed to a single projection — 10.00;
      * a closed-form jump to the first in-window date instead of iterating —
        lands off the weekly grid and the count and the dates both move;
      * the shipped ``next_due_date > today`` gate — 0.00.

    The category breakdown is asserted to SUM to the total, because
    ``ai_forecast_refine_service`` consumes the breakdown as the baseline it
    hands the model. Totals that move without the breakdown moving make that
    baseline internally inconsistent.
    """
    today = datetime.date.today()
    seed, p_start, window_end = await _seed_on_grid(db_session, today=today, days_before=10)
    frontier = p_start - datetime.timedelta(days=11)

    # Fixture preconditions.
    assert p_start + datetime.timedelta(days=24) <= window_end
    assert p_start + datetime.timedelta(days=31) > window_end

    db_session.add(_template(
        seed, amount=Decimal("10.00"), frequency="weekly", next_due_date=frontier,
    ))
    await db_session.commit()

    before = await forecast_service.compute_forecast(
        db_session, seed["org_id"], today=today
    )
    assert Decimal(before["recurring_expense"]) == Decimal("40")   # 4 x 10, not 6 or 1
    assert Decimal(before["pending_expense"]) == Decimal("0")
    assert sum(
        Decimal(c["recurring"]) for c in before["categories"]
    ) == Decimal(before["recurring_expense"])

    res = await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=today
    )
    assert res["generated"] == 6
    rows = await _rows(db_session, seed["org_id"])
    assert [r.date for r in rows] == [
        frontier + datetime.timedelta(weeks=k) for k in range(6)
    ]
    assert len([r for r in rows if r.date < p_start]) == 2

    after = await forecast_service.compute_forecast(
        db_session, seed["org_id"], today=today
    )
    assert Decimal(after["pending_expense"]) == Decimal("40")
    assert Decimal(after["recurring_expense"]) == Decimal("0")
    assert Decimal(before["forecast_net"]) == Decimal("-40")
    assert Decimal(after["forecast_net"]) == Decimal("-40")


# ─────────────────────────────────────────────────────────────────────────────
# F10c — fence. The projection horizon is ``window_end``, NOT the
#        materialisation window.
# ─────────────────────────────────────────────────────────────────────────────

async def test_f10c_projection_horizon_is_window_end_not_cycle_end(db_session):
    """FENCE. Kills the most plausible WRONG fix.

    On a lapsed roster the forecast window stops at ``today`` while
    ``current_cycle_window`` runs weeks past it. The tempting inference —
    "generation materialises through ``current_cycle_window(...)[1]``, so the
    projection should run to ``cycle_end`` too" — is wrong: ``recurring_*`` is a
    bucket OF THIS PERIOD, and the occurrences past ``window_end`` belong to the
    successor's forecast, where ``pending_*`` will report them once they are
    materialised.

    Weekly 10.00 due ``today - 3``. In ``[p_start, today]`` there is exactly ONE
    occurrence; in ``[p_start, cycle_end]`` there are five.

    Wrong implementations killed:
      * projecting to ``current_cycle_window(cycle_day, today)[1]`` — 50.00;
      * the shipped ``next_due_date > today`` gate — 0.00, and the net moves;
      * a probe bounded on ``[p_start, cycle_end]`` rather than the window — the
        rows generation writes past ``window_end`` would suppress nothing here,
        but the sums would still disagree with the successor's.
    """
    today = datetime.date.today() - datetime.timedelta(days=40)
    p_start = today - datetime.timedelta(days=90)
    cycle_day = min(today.day, 28)
    _, cycle_end = current_cycle_window(cycle_day, today)
    seed = await _seed(
        db_session,
        open_start=p_start,
        closed_windows=(
            (today - datetime.timedelta(days=60), today - datetime.timedelta(days=31)),
            (today - datetime.timedelta(days=30), today - DAY),
        ),
        cycle_day=cycle_day,
    )
    # Fixture precondition: the two horizons genuinely differ, by more than one
    # weekly step. Without this the fence cannot distinguish them.
    assert cycle_end >= today + datetime.timedelta(days=4)

    db_session.add(_template(
        seed, amount=Decimal("10.00"), frequency="weekly",
        next_due_date=today - datetime.timedelta(days=3),
    ))
    await db_session.commit()

    before = await forecast_service.compute_forecast(
        db_session, seed["org_id"], today=today
    )
    assert before["period_end"] == today.isoformat()
    assert Decimal(before["recurring_expense"]) == Decimal("10")   # ONE, not five

    res = await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=today
    )
    # Anti-vacuity: generation really does reach past window_end.
    assert res["generated"] > 1
    rows = await _rows(db_session, seed["org_id"])
    assert len([r for r in rows if r.date > today]) >= 1

    after = await forecast_service.compute_forecast(
        db_session, seed["org_id"], today=today
    )
    assert Decimal(after["pending_expense"]) == Decimal("10")
    assert Decimal(after["recurring_expense"]) == Decimal("0")
    assert Decimal(before["forecast_net"]) == Decimal("-10")
    assert Decimal(after["forecast_net"]) == Decimal("-10")


# ─────────────────────────────────────────────────────────────────────────────
# F15 — fence. Conservation is about ``forecast_expense``, not ``pending_*``.
# ─────────────────────────────────────────────────────────────────────────────

async def test_f15_auto_settle_overdue_template_conserves_via_executed(db_session):
    """FENCE. An ``auto_settle`` overdue template lands in ``executed_*``.

    ``generate_due_transactions`` writes an ``auto_settle`` instance whose date
    has passed straight to SETTLED. So the amount leaves ``recurring_*`` and
    arrives in ``executed_*`` — ``pending_*`` is 0.00 on BOTH sides.

    Wrong implementations killed:
      * any fix that projects only non-``auto_settle`` templates —
        ``before.recurring`` is 0 and ``forecast_expense`` moves 0 -> 100;
      * any conservation assertion written over ``pending_*`` alone: it would be
        RED against the CORRECT implementation here
        (``reference_over_specified_test_false_red``). Recorded so the next
        person does not "fix" this test by asserting pending.
    """
    today = datetime.date.today()
    seed, p_start, window_end = await _seed_on_grid(db_session, today=today, days_before=5)
    due = today - datetime.timedelta(days=3)

    # Fixture preconditions.
    assert p_start <= due < today
    assert due + relativedelta(months=1) > window_end    # exactly ONE occurrence

    db_session.add(_template(seed, next_due_date=due, auto_settle=True))
    await db_session.commit()

    before = await forecast_service.compute_forecast(
        db_session, seed["org_id"], today=today
    )
    assert Decimal(before["recurring_expense"]) == Decimal("100")
    assert Decimal(before["executed_expense"]) == Decimal("0")
    assert Decimal(before["pending_expense"]) == Decimal("0")

    res = await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=today
    )
    assert res["generated"] == 1
    rows = await _rows(db_session, seed["org_id"])
    assert [(r.status, r.date) for r in rows] == [(TransactionStatus.SETTLED, due)]

    after = await forecast_service.compute_forecast(
        db_session, seed["org_id"], today=today
    )
    assert Decimal(after["executed_expense"]) == Decimal("100")
    assert Decimal(after["pending_expense"]) == Decimal("0")
    assert Decimal(after["recurring_expense"]) == Decimal("0")

    # The conservation claim, stated over the totals it is actually about.
    assert Decimal(after["forecast_expense"]) == Decimal(before["forecast_expense"])
    assert Decimal(after["forecast_expense"]) == Decimal("100")
    assert Decimal(after["forecast_net"]) == Decimal(before["forecast_net"])


# ─────────────────────────────────────────────────────────────────────────────
# F16 — fence. The anti-double-count probe, with the frontier REWOUND, and both
#       of the probe's date bounds pinned.
# ─────────────────────────────────────────────────────────────────────────────

async def test_f16_probe_suppresses_already_materialised_occurrences(db_session):
    """FENCE. Without a probe, dropping the clock gate DOUBLE-COUNTS.

    Once ``recurring_*`` is bounded by the occurrence rather than by the
    frontier, a template whose frontier has NOT advanced past the window is
    selected even though its occurrence is already a row. ``PATCH /recurring``
    reaches that state directly: ``RecurringUpdate.next_due_date`` has no
    validator and ``update_recurring`` assigns it verbatim, so a user who
    rewinds the schedule of a template that already generated re-creates exactly
    this shape (the same shape
    ``test_recurring_generate_fill_period.test_dedup_guard_...`` uses).

    Three templates, three materialised occurrences at three positions —
    ``p_start`` (the probe's LOWER bound), an interior date, and ``window_end``
    (the probe's UPPER bound) — with distinct amounts so a failure names the
    bound that broke.

    Wrong implementations killed:
      * no probe at all — 1110 projected on top of 1110 pending = 2220;
      * ``Transaction.date > p_start`` for ``>=`` in the probe — the 10.00
        materialised ON ``p_start`` escapes the probe and is projected: 1120;
      * ``Transaction.date < window_end`` for ``<=`` — the 1000.00 materialised
        ON ``window_end`` escapes: 2110.

    ⚠ The fixture must leave the frontier REWOUND. If ``next_due`` is allowed to
    stay advanced, the template is dropped by the query's
    ``next_due_date <= window_end`` gate, the probe is never consulted, and the
    test is green under every implementation above.
    """
    today = datetime.date.today()
    seed, p_start, window_end = await _seed_on_grid(db_session, today=today, days_before=5)
    interior = today + datetime.timedelta(days=3)

    # Three distinct positions, three distinct amounts.
    placements = [
        (p_start, Decimal("10.00")),
        (interior, Decimal("100.00")),
        (window_end, Decimal("1000.00")),
    ]
    assert p_start < interior < window_end
    templates = []
    for due, amount in placements:
        t = _template(seed, amount=amount, next_due_date=due)
        db_session.add(t)
        templates.append(t)
    await db_session.commit()

    res = await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=today
    )
    assert res["generated"] == 3
    rows = await _rows(db_session, seed["org_id"])
    assert [r.date for r in rows] == [p_start, interior, window_end]

    # Rewind every frontier through the real update path.
    for t, (due, _amount) in zip(templates, placements):
        await recurring_service.update_recurring(
            db_session, seed["org_id"], t.id,
            RecurringUpdate(next_due_date=due),
        )
    # Precondition: every template is still SELECTED by the query gate, so the
    # probe is genuinely the thing under test.
    for t, (due, _amount) in zip(templates, placements):
        assert t.next_due_date == due <= window_end

    fc = await forecast_service.compute_forecast(
        db_session, seed["org_id"], today=today
    )

    assert Decimal(fc["recurring_expense"]) == Decimal("0")
    assert Decimal(fc["pending_expense"]) == Decimal("1110")
    assert Decimal(fc["forecast_expense"]) == Decimal("1110")   # NOT 2220
    # ⚠ `all(())` is True. The non-emptiness assert is what stops this line
    # from being decoration (`reference_self_review_without_copilot`).
    assert fc["categories"]
    assert all(Decimal(c["recurring"]) == Decimal("0") for c in fc["categories"])


# ─────────────────────────────────────────────────────────────────────────────
# F17 — fence. The two walks truncate identically, and both are ITERATED.
# ─────────────────────────────────────────────────────────────────────────────

def test_f17_occurrence_walk_matches_generation_walk():
    """FENCE. One iteration budget, one grid.

    Two claims, both structural:

    1. ``date_utils.MAX_OCCURRENCE_ITERATIONS is
       recurring_service.MAX_CATCHUP_ITERATIONS``. If they were two independent
       literals, a pathologically stale template would truncate at different
       points in the projection and in generation, and ``forecast_net`` would
       move with no user action — the exact defect TBD-260 removes, reintroduced
       through the back door.
    2. ``occurrences_in_window`` walks with ``advance_date``, never closed-form.

    ⚠ **The fixture MUST start on the 31st.** ``advance_date`` is
    path-dependent at month ends: Jan 31 -> Feb 28 -> Mar 28 -> Apr 28, NOT
    Mar 31 / Apr 30. A non-month-end fixture makes the closed-form answer and
    the iterated answer identical and the fence vacuous. The assertion below
    that ``2026-03-31`` is absent while ``2026-03-28`` is present is what makes
    that discrimination explicit.

    Fixed literals, not ``today ± n``: this test is about the calendar itself,
    and 2026 is deliberately a non-leap year so ``Feb 28`` is stable.

    Wrong implementations killed:
      * a closed-form first-in-window jump (``next_due + ceil(...) months``) —
        yields ``Mar 31 / Apr 30 / May 31``;
      * ``MAX_CATCHUP_ITERATIONS = 500`` re-declared as its own literal. ⚠ A
        VALUE comparison cannot see this — the two are equal the day it is
        written and drift silently later. The equality below is therefore
        backed by an AST guard over ``recurring_service``'s own source, which
        asserts the right-hand side of that assignment is a NAME and not a
        constant. There is no type checker in CI to catch it otherwise.
    """
    assert recurring_service.MAX_CATCHUP_ITERATIONS == MAX_OCCURRENCE_ITERATIONS

    tree = ast.parse(inspect.getsource(recurring_service))
    binding = [
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "MAX_CATCHUP_ITERATIONS"
            for t in node.targets
        )
    ]
    assert len(binding) == 1, "MAX_CATCHUP_ITERATIONS must be bound exactly once"
    rhs = binding[0].value
    assert isinstance(rhs, ast.Name), (
        "MAX_CATCHUP_ITERATIONS must ALIAS date_utils.MAX_OCCURRENCE_ITERATIONS, "
        f"not re-declare a literal (got {ast.dump(rhs)})"
    )
    assert rhs.id == "MAX_OCCURRENCE_ITERATIONS"

    next_due = datetime.date(2026, 1, 31)
    start = datetime.date(2026, 2, 1)
    end = datetime.date(2026, 5, 31)

    got = occurrences_in_window(next_due, Frequency.MONTHLY, start, end)

    # The reference: exactly what iterating advance_date from the same origin
    # produces, computed independently of the helper.
    reference, d = [], next_due
    while d <= end:
        if d >= start:
            reference.append(d)
        d = advance_date(d, Frequency.MONTHLY)

    assert got == reference
    assert got == [
        datetime.date(2026, 2, 28),
        datetime.date(2026, 3, 28),
        datetime.date(2026, 4, 28),
        datetime.date(2026, 5, 28),
    ]
    # The discrimination, stated: the closed-form grid is NOT this grid.
    assert datetime.date(2026, 3, 31) not in got
    assert datetime.date(2026, 3, 28) in got

    # The budget spans BOTH loops from the same origin, so a walk that would
    # need more steps than the cap truncates rather than running away.
    capped = occurrences_in_window(
        next_due, Frequency.MONTHLY, start, end, max_iterations=2,
    )
    assert capped == [datetime.date(2026, 2, 28)]


# ─────────────────────────────────────────────────────────────────────────────
# F18 — fence. The probe is KEY-EXISTENCE, not "is it counted". The fence for
#       the contested ruling, and for a LIVE double-count on ``main``.
# ─────────────────────────────────────────────────────────────────────────────

async def test_f18_probe_is_key_existence_not_reportability(db_session):
    """FENCE. The probe must NOT carry ``reportable_transaction_filter()``.

    ``generate_due_transactions``'s own create-condition matches on
    ``(org_id, recurring_id, date)`` with NO status, NO reportability and NO
    effective-date term. A probe narrower than that projects an occurrence
    generation will never materialise, and the projected value then self-clears
    at the next scheduler tick — the defect this ticket removes.

    The fixture is the state ``promote_to_recurring`` + ``/transactions/pair``
    produce, all through real service calls:

      1. a PENDING expense at ``D`` and a mirror PENDING income at ``D`` on a
         second account;
      2. "Repeats" ticked -> ``promote_to_recurring`` clones the row into a
         template with ``next_due_date == tx.date`` (the UI sends
         ``date < today ? today : date``, so a future-dated row yields
         ``next_due == date``);
      3. the two legs are paired -> both carry ``linked_transaction_id`` and
         drop out of every reportable aggregate.

    Wrong implementations killed:
      * a probe carrying ``reportable_transaction_filter()`` — the paired row is
        invisible to it, the occurrence is projected, and the user sees a
        phantom 100.00 expense for a TRANSFER, which then vanishes on its own;
      * no probe at all — same 100.00.

    ⚠ **Do not assert ``forecast_net`` alone.** The paired income leg makes the
    net zero under both implementations. ``recurring_expense`` is asserted by
    name, and so is the category breakdown.

    ⚠ **This shape is a LIVE double-count on ``main``**, independent of pairing:
    for ``D > today`` the shipped ``> today`` gate PASSES, so ``main`` counts
    ``D`` in ``recurring_*`` while the promoted row is already in ``pending_*``
    — one transaction, twice, in ``forecast_expense``. The probe fixes it as a
    side effect. Unfenced before this test.
    """
    today = datetime.date.today()
    seed, p_start, window_end = await _seed_on_grid(db_session, today=today, days_before=5)
    D = today + datetime.timedelta(days=3)

    expense_tx = _pending(seed, "100.00", D)
    income_tx = _pending(
        seed, "100.00", D,
        account_id=seed["account_b_id"], category_id=seed["cat_income"],
        type=TransactionType.INCOME,
    )
    db_session.add_all([expense_tx, income_tx])
    await db_session.commit()

    promoted = await transaction_service.promote_to_recurring(
        db_session, seed["org_id"], expense_tx.id,
        PromoteToRecurringRequest(
            frequency="monthly", next_due_date=D, auto_settle=False
        ),
    )
    template = (await db_session.execute(
        select(RecurringTransaction).where(
            RecurringTransaction.id == promoted.recurring_id
        )
    )).scalar_one()

    await transaction_service.pair_existing_transactions(
        db_session, seed["org_id"], expense_tx.id, income_tx.id
    )

    # Preconditions — mandatory, or the fence is decoration.
    assert template.next_due_date == expense_tx.date == D
    assert p_start <= D <= window_end
    assert expense_tx.linked_transaction_id is not None
    assert income_tx.linked_transaction_id is not None
    assert expense_tx.recurring_id == template.id

    fc = await forecast_service.compute_forecast(
        db_session, seed["org_id"], today=today
    )

    assert fc["period_end"] == window_end.isoformat()
    assert Decimal(fc["recurring_expense"]) == Decimal("0")
    assert Decimal(fc["pending_expense"]) == Decimal("0")
    assert Decimal(fc["forecast_expense"]) == Decimal("0")
    # ⚠ NOT `all(Decimal(c["recurring"]) == 0 for c in ...)` — `all(())` is True
    # and the breakdown here is legitimately EMPTY (nothing executed, nothing
    # reportable-pending, nothing projected), so that phrasing would be pure
    # decoration. State the emptiness instead: a probe carrying
    # `reportable_transaction_filter()` projects 100.00 and CREATES a row here.
    assert fc["categories"] == []


# ─────────────────────────────────────────────────────────────────────────────
# F19 — fence. The effective-date bound belongs to the SUMS, not the probe.
# ─────────────────────────────────────────────────────────────────────────────

async def test_f19_probe_is_not_bounded_on_the_effective_period_date(db_session):
    """FENCE. The other half of the contested ruling.

    A promoted PENDING row that is fully reportable, but whose ``settled_date``
    estimate pushes its EFFECTIVE period past ``window_end``: it is counted in
    the SUCCESSOR period's ``pending_*``, not this one's. The occurrence is
    nonetheless materialised — ``Transaction.date`` is inside this window — so
    the probe, which asks only "does a row for this occurrence exist", suppresses
    the projection here.

    Wrong implementation killed: a probe bounded on
    ``effective_period_date_expr()`` instead of ``Transaction.date``. The row's
    effective date is outside ``[p_start, window_end]``, so it is invisible to
    that probe, 100.00 is projected HERE, and the successor ALSO carries 100.00
    in ``pending_*``: 200.00 across the two periods for one obligation. The
    projected half then self-clears at the next tick.

    ⚠ Without the successor-period assertion this degenerates into "the amount
    vanished". The two-period sum is the whole content of the test.

    ⚠ YEARLY, not monthly. A monthly template has a GENUINE second occurrence
    inside the successor's window, so the two-period sum is 200.00 under the
    correct implementation too and the fence cannot discriminate.
    """
    today = datetime.date.today()
    seed, p_start, window_end = await _seed_on_grid(db_session, today=today, days_before=5)
    successor_start = window_end + DAY
    successor_end = window_end + datetime.timedelta(days=30)
    D = today + datetime.timedelta(days=3)
    settles = window_end + datetime.timedelta(days=10)

    tx = _tx(
        seed, amount=Decimal("100.00"), status=TransactionStatus.PENDING,
        date=D, settled_date=settles,
    )
    db_session.add(tx)
    await db_session.commit()

    promoted = await transaction_service.promote_to_recurring(
        db_session, seed["org_id"], tx.id,
        PromoteToRecurringRequest(
            frequency="yearly", next_due_date=D, auto_settle=False
        ),
    )

    # Preconditions.
    assert tx.settled_date > window_end and tx.settled_date >= tx.date
    assert p_start <= tx.date <= window_end
    assert successor_start <= settles <= successor_end
    assert promoted.recurring_id is not None

    this = await forecast_service.compute_forecast(
        db_session, seed["org_id"], period_start=p_start, today=today
    )
    nxt = await forecast_service.compute_forecast(
        db_session, seed["org_id"], period_start=successor_start, today=today
    )

    assert this["period_end"] == window_end.isoformat()
    assert nxt["period_end"] == successor_end.isoformat()

    # This period: the row's effective date is out, and the occurrence it came
    # from is suppressed by the probe.
    assert Decimal(this["recurring_expense"]) == Decimal("0")
    assert Decimal(this["pending_expense"]) == Decimal("0")
    assert Decimal(this["forecast_expense"]) == Decimal("0")

    # The successor: the amount lands, exactly once.
    assert Decimal(nxt["pending_expense"]) == Decimal("100")
    assert Decimal(nxt["recurring_expense"]) == Decimal("0")

    # THE fence. One obligation, one hundred, across both periods.
    assert (
        Decimal(this["forecast_expense"]) + Decimal(nxt["forecast_expense"])
    ) == Decimal("100")
