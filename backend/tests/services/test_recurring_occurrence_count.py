"""Instalment series: a recurring template that delivers N occurrences (TBD-275).

``RecurringTransaction`` grows two columns -- ``occurrence_count`` (declared
intent, NULL = open-ended) and ``occurrences_elapsed`` (stored progress) -- and
FIVE sites that read templates must agree about how many occurrences the series
has left. They are:

    forecast_service.compute_forecast                 (projects)
    recurring_service.generate_due_transactions       (materialises)
    forecast_plan_service.populate_from_sources       (projects)
    scenario_engine.build_state / _project            (projects)
    scheduler/jobs/recurring_generation.is_due        (decides to wake)

The governing invariant is TBD-260's, unchanged: the occurrences a projection
counts and the occurrences generation creates are the SAME SET. So a budget
applied to one walker and not the other does not merely mis-count -- it makes
``forecast_net`` MOVE across a generation run, with no user action.

⭐ **The subtle half is the fast-forward.** ``occurrences_in_window`` has two
loops: one that advances past occurrences before ``start`` and DISCARDS them,
and one that collects. The discarded ones are REAL -- ``generate_due_transactions``'
catch-up loop has no lower bound and materialises every one of them -- so they
spend the series budget, and the projection must spend it too. F-B is the fence
for exactly that, and it is the one a plausible implementation fails.

Every test here is a ``fence``: its docstring names the wrong implementation it
goes RED against, and every one was injected into the source and confirmed RED
before this file was committed (``reference_vacuous_test_pattern``).

Clocks are injected everywhere and fixtures anchor to ``date.today() ± n``
rather than to literals (``reference_wall_clock_date_bomb_tests``). Where a
quantity depends on month length, the assertions are chosen so they do not
swing with it -- see ``_WEEKLY_FLOOR`` (TBD-296's fixture-geometry rule).
"""
from __future__ import annotations

import ast
import datetime
import inspect
from decimal import Decimal

import pytest
import pytest_asyncio
from dateutil.relativedelta import relativedelta
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization
from app.models.account import PaymentStrategy
from app.models.base import Base
from app.models.billing import BillingPeriod
from app.models.category import CategoryType
from app.models.recurring import Frequency, RecurringTransaction
from app.models.scenario import Scenario, ScenarioType
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.schemas.recurring import RecurringCreate, RecurringUpdate
from app.schemas.transaction import PromoteToRecurringRequest
from app.services import (
    forecast_plan_service,
    forecast_service,
    recurring_service,
    scenario_engine,
    transaction_service,
)
from app.services.account_balance_forecast_service import (
    compute_account_balance_forecast,
)
from app.services.billing_service import current_cycle_window
from app.services.date_utils import advance_date, occurrences_in_window
from app.services.recurring_filters import (
    active_series_filter,
    has_remaining_occurrences,
    remaining_occurrences,
)
from app.services.scenario_engine import AnalyticEngine, SimulationRequest
from app.services.scheduler.jobs import recurring_generation

DAY = datetime.timedelta(days=1)

# A billing cycle is 28..31 days long, so a WEEKLY grid anchored on ``p_start``
# always has occurrences at +0, +7, +14 and +21 inside the window, and MAY have
# one at +28. Every weekly assertion below is therefore chosen to be
# independent of which -- a budget of 3 bites in both worlds, a budget of 5
# would bite in only one (TBD-296).
_WEEKLY_FLOOR = 4


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


# ─── fixture scaffolding ─────────────────────────────────────────────────────

def _safe_month_anchor(d: datetime.date) -> datetime.date:
    """Nudge ``d`` back to a day-of-month that exists in EVERY month (<= 28).

    ``advance_date`` is path-dependent at month ends (Jan 31 -> Feb 28 -> Mar
    28), so ``p_start + k months`` is only a clean monthly grid when
    ``p_start.day <= 28``. Every multi-cycle fixture here relies on that
    identity; without the anchor they would be date bombs firing only in the
    last three days of a long month.
    """
    while d.day > 28:
        d -= DAY
    return d


async def _seed(db: AsyncSession, *, p_start: datetime.date) -> dict:
    """One org whose billing cycle day matches ``p_start``, an OPEN period at
    ``p_start``, a checking account and a CREDIT CARD account.

    No closed successor period: the open row's effective end is then the
    calendar fallback ``p_start + 1 month - 1 day``, and
    ``period_spend_window_end`` (which takes ``max(end, today)``) leaves it
    there for any ``today`` inside the cycle. That makes the forecast window and
    ``current_cycle_window`` -- the window generation materialises over --
    COINCIDE, which every conservation claim below depends on and
    ``_assert_geometry`` re-checks at each cycle.
    """
    org = Organization(name="T", billing_cycle_day=p_start.day)
    db.add(org)
    await db.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    cc_type = AccountType(
        org_id=org.id, name="Credit Card", slug="credit_card", is_system=True
    )
    db.add_all([at, cc_type])
    await db.flush()
    acct = Account(
        org_id=org.id, name="Main", account_type_id=at.id,
        balance=Decimal("1000.00"), currency="EUR", is_default=True,
    )
    cc = Account(
        org_id=org.id, name="Visa", account_type_id=cc_type.id,
        balance=Decimal("0.00"), currency="EUR", is_default=False,
    )
    db.add_all([acct, cc])
    await db.flush()
    cat = Category(org_id=org.id, name="Food", slug="food", type=CategoryType.EXPENSE)
    cat_b = Category(
        org_id=org.id, name="Loans", slug="loans", type=CategoryType.EXPENSE
    )
    cat_income = Category(
        org_id=org.id, name="Salary", slug="salary", type=CategoryType.INCOME
    )
    db.add_all([cat, cat_b, cat_income])
    await db.flush()
    db.add(BillingPeriod(org_id=org.id, start_date=p_start))
    await db.commit()
    return {
        "org_id": org.id,
        "account_id": acct.id,
        "cc_account_id": cc.id,
        "account_type_id": at.id,
        "cc_type_id": cc_type.id,
        "cat_id": cat.id,
        "cat_b": cat_b.id,
        "cat_income": cat_income.id,
        "cycle_day": p_start.day,
    }


def _template(seed: dict, **overrides) -> RecurringTransaction:
    defaults = dict(
        org_id=seed["org_id"],
        account_id=seed["account_id"],
        category_id=seed["cat_id"],
        description="instalment",
        amount=Decimal("10.00"),
        type="expense",
        frequency="monthly",
        auto_settle=False,
        is_active=True,
        occurrences_elapsed=0,
    )
    defaults.update(overrides)
    return RecurringTransaction(**defaults)


def _assert_geometry(
    seed: dict, today: datetime.date, p_start: datetime.date
) -> datetime.date:
    """Pin, not assume, that the two windows coincide for this cycle.

    Returns ``window_end``. Asserted at every step of every multi-cycle loop:
    a conservation claim is a claim about the projection window and the
    materialisation window being the same interval, and a fixture that quietly
    drifts them apart proves nothing.
    """
    got_start, got_end = current_cycle_window(seed["cycle_day"], today)
    assert got_start == p_start, f"cycle drifted: {got_start} != {p_start}"
    assert got_end == p_start + relativedelta(months=1) - DAY
    assert p_start <= today <= got_end
    return got_end


async def _roll_cycle(
    db: AsyncSession, org_id: int, p_start: datetime.date, window_end: datetime.date
) -> datetime.date:
    """Close the open period and open its successor, as ``BillingCloseJob`` does.

    Returns the new ``p_start``. Rolling the roster explicitly (rather than
    letting ``get_current_period`` auto-create) is what lets these tests step
    ``today`` across several cycles while keeping the forecast window and
    ``current_cycle_window`` in agreement at every step.
    """
    res = await db.execute(
        select(BillingPeriod).where(
            BillingPeriod.org_id == org_id, BillingPeriod.end_date.is_(None)
        )
    )
    open_p = res.scalar_one()
    assert open_p.start_date == p_start
    open_p.end_date = window_end
    new_start = window_end + DAY
    db.add(BillingPeriod(org_id=org_id, start_date=new_start))
    await db.commit()
    return new_start


async def _rows(db: AsyncSession, org_id: int) -> list[Transaction]:
    res = await db.execute(
        select(Transaction)
        .where(Transaction.org_id == org_id)
        .order_by(Transaction.date, Transaction.id)
    )
    return list(res.scalars().all())


async def _reload(db: AsyncSession, template_id: int) -> RecurringTransaction:
    res = await db.execute(
        select(RecurringTransaction)
        .where(RecurringTransaction.id == template_id)
        .execution_options(populate_existing=True)
    )
    return res.scalar_one()


# ─────────────────────────────────────────────────────────────────────────────
# F-A — fence. CONSERVATION across a generation run, budget in BOTH walkers.
# ─────────────────────────────────────────────────────────────────────────────

async def test_fa_conservation_across_generation_for_every_cycle(db_session):
    """FENCE. A budget in ONE walker makes ``forecast_net`` move on its own.

    A WEEKLY 3-instalment series stepped across four billing cycles. At every
    cycle: snapshot the forecast, run generation, re-snapshot, assert
    ``forecast_net`` is unchanged. That equality is the whole TBD-260 invariant
    -- the occurrences a projection counts and the ones generation creates are
    one set, so materialising moves an amount BETWEEN buckets and never into or
    out of the total.

    ⚠ **It must be WEEKLY, and the budget must be smaller than the window.** A
    monthly template puts exactly ONE occurrence in a monthly window, so the
    budget never binds inside a single cycle and the exhausted series is caught
    by ``active_series_filter`` in the query instead -- the fence would then be
    green against an unbudgeted walker. With 3 instalments against at least
    ``_WEEKLY_FLOOR`` = 4 in-window occurrences the BUDGET is provably the thing
    doing the work, and that is asserted below rather than assumed.

    ⚠ **Assert on ``forecast_net``, not ``recurring_expense``.**
    ``auto_settle=True`` migrates the value into ``executed_*``/``pending_*``,
    so ``recurring_expense`` legitimately drops to 0 across the run; only the
    total conserves.

    ⚠ The k = N steps (cycles after the series finishes) matter most: they are
    where an exhausted series that is still projected, or still generated,
    shows up as a net that moves from nothing.

    Wrong implementations killed:
      * no ``budget=`` at ``forecast_service``'s ``occurrences_in_window``
        call -- the projection counts every in-window occurrence, generation
        stops at 3, and cycle 0 moves -40 -> -30;
      * no ``has_remaining_occurrences`` guard in ``generate_due_transactions``'
        catch-up loop -- generation creates 4+, the projection said 3, and
        cycle 0 moves -30 -> -40;
      * ``active_series_filter`` missing from ``forecast_service`` -- an
        exhausted series keeps projecting and every later cycle moves;
      * ``active_series_filter`` missing from ``generate_due_transactions`` --
        symmetric, in the other direction.
    """
    today = datetime.date.today()
    p_start = _safe_month_anchor(today - datetime.timedelta(days=5))
    seed = await _seed(db_session, p_start=p_start)
    count = 3

    t = _template(
        seed, frequency="weekly", next_due_date=p_start,
        occurrence_count=count, auto_settle=True,
    )
    db_session.add(t)
    await db_session.commit()
    template_id = t.id

    cycle_start = p_start
    for k in range(count + 1):
        clock = cycle_start + datetime.timedelta(days=5)
        window_end = _assert_geometry(seed, clock, cycle_start)

        if k == 0:
            # ⚠ ANTI-VACUITY. Without this the fence cannot distinguish
            # "the budget stopped the walk" from "the window did", and an
            # unbudgeted projection would agree with generation by accident.
            unbudgeted = occurrences_in_window(
                p_start, Frequency.WEEKLY, cycle_start, window_end
            )
            assert len(unbudgeted) >= _WEEKLY_FLOOR > count

        before = await forecast_service.compute_forecast(
            db_session, seed["org_id"], today=clock
        )
        await recurring_service.generate_due_transactions(
            db_session, seed["org_id"], today=clock
        )
        after = await forecast_service.compute_forecast(
            db_session, seed["org_id"], today=clock
        )

        assert Decimal(after["forecast_net"]) == Decimal(before["forecast_net"]), (
            f"forecast_net moved across generation in cycle {k}: "
            f"{before['forecast_net']} -> {after['forecast_net']}"
        )

        if k == 0:
            # The series is delivered in full in cycle 0 and the total is
            # exactly the budget, never the window.
            assert Decimal(before["forecast_net"]) == Decimal("-30")
            assert len(await _rows(db_session, seed["org_id"])) == count
        else:
            # k >= 1: exhausted. Nothing projected, nothing created, net 0.
            assert Decimal(before["forecast_net"]) == Decimal("0")
            assert Decimal(before["recurring_expense"]) == Decimal("0")
            assert len(await _rows(db_session, seed["org_id"])) == count

        if k < count:
            cycle_start = await _roll_cycle(
                db_session, seed["org_id"], cycle_start, window_end
            )

    t = await _reload(db_session, template_id)
    assert t.occurrences_elapsed == count


# ─────────────────────────────────────────────────────────────────────────────
# F-B — ⭐ fence. THE FAST-FORWARD SPENDS BUDGET. The one a plausible
#       implementation fails.
# ─────────────────────────────────────────────────────────────────────────────

async def test_fb_fast_forward_loop_spends_the_series_budget(db_session):
    """FENCE. Budgeting only the COLLECT loop is the natural implementation
    and it is wrong. This is the test that says so.

    A WEEKLY 2-instalment series whose frontier sits TWO occurrences before
    ``p_start``. Those two occurrences are before the window, so
    ``occurrences_in_window``'s fast-forward loop walks past them and throws
    them away -- but they are REAL: ``generate_due_transactions``' catch-up
    loop has no lower bound and materialises both, spending the series' entire
    budget before the window is even reached. The correct projection for the
    window is therefore ZERO.

    Budget the collect loop only and the fast-forward runs free: the walk
    arrives at ``p_start`` with all 2 instalments still in hand and projects
    two occurrences that generation will NEVER create. ``forecast_net`` then
    reads -20 before the run and 0 after it -- it moves, with no user action,
    on the scheduler's tick. Exactly the TBD-260 defect class this ticket was
    written to avoid re-introducing.

    ⚠ **Two fixture properties are both required, and each one alone makes the
    fence vacuous:**

      * the frontier must be STRICTLY BEFORE ``p_start`` -- with the frontier
        inside the window the fast-forward makes ZERO passes, both
        implementations agree, and the test is green against the mutant;
      * the window must hold at least TWO further occurrences -- so the leak
        under the mutant is two whole occurrences, not a one-off boundary
        coincidence.

    ⚠ **A ROW-COUNT fence cannot see this bug and neither can a same-window
    fixture.** The mutant lives entirely inside the projection: generation
    creates the same 2 rows either way, at the same 2 dates, and
    ``occurrences_elapsed`` reaches 2 either way. Both are asserted below
    precisely to show that they do NOT discriminate -- the discriminating
    assertion is ``recurring_expense == 0``.

    Wrong implementations killed:
      * ``budget`` spent by the collect loop only -- ``recurring_expense`` is
        20.00 and ``forecast_net`` moves -20 -> 0;
      * ``budget`` ignored entirely -- same numbers, plus the later
        occurrences.
    """
    today = datetime.date.today()
    p_start = _safe_month_anchor(today - datetime.timedelta(days=5))
    seed = await _seed(db_session, p_start=p_start)
    window_end = _assert_geometry(seed, today, p_start)

    frontier = p_start - datetime.timedelta(days=14)
    count = 2

    # Fixture preconditions. Without these the fence is decoration.
    assert frontier < p_start
    # Exactly ``count`` occurrences strictly before the window: the budget is
    # spent precisely by the fast-forward, with nothing left over.
    pre_window = [frontier, frontier + datetime.timedelta(days=7)]
    assert all(d < p_start for d in pre_window)
    assert len(pre_window) == count
    assert advance_date(pre_window[-1], Frequency.WEEKLY) == p_start
    # ...and the window itself holds >= 2 more, so the mutant leaks two whole
    # occurrences rather than grazing a boundary.
    assert len(occurrences_in_window(
        p_start, Frequency.WEEKLY, p_start, window_end
    )) >= _WEEKLY_FLOOR

    t = _template(
        seed, frequency="weekly", next_due_date=frontier, occurrence_count=count,
    )
    db_session.add(t)
    await db_session.commit()
    template_id = t.id

    before = await forecast_service.compute_forecast(
        db_session, seed["org_id"], today=today
    )
    # ⭐ THE discriminating assertion. 0.00, not 20.00.
    assert Decimal(before["recurring_expense"]) == Decimal("0")
    assert Decimal(before["forecast_net"]) == Decimal("0")

    res = await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=today
    )

    # ⚠ These three do NOT discriminate -- they are identical under the mutant.
    # They are here to prove the fixture really does reach below ``p_start``,
    # i.e. that the fast-forward has something to spend.
    assert res["generated"] == count
    rows = await _rows(db_session, seed["org_id"])
    assert [r.date for r in rows] == pre_window
    assert all(r.date < p_start for r in rows)
    assert (await _reload(db_session, template_id)).occurrences_elapsed == count

    after = await forecast_service.compute_forecast(
        db_session, seed["org_id"], today=today
    )
    assert Decimal(after["forecast_net"]) == Decimal(before["forecast_net"])
    assert Decimal(after["forecast_net"]) == Decimal("0")


# ─────────────────────────────────────────────────────────────────────────────
# F-C — fence. Neither over- nor under-generation, across many cycles.
# ─────────────────────────────────────────────────────────────────────────────

async def test_fc_three_instalments_across_six_cycles_produce_exactly_three_rows(
    db_session,
):
    """FENCE. A 3-instalment monthly plan run for SIX cycles yields 3 rows.

    Six cycles for a 3-instalment plan: the series must stop half way and stay
    stopped. Both directions are pinned -- ``== 3`` fails high against a
    missing budget (6 rows) and fails low against an off-by-one
    (``remaining >= 0`` instead of ``> 0``, or the ``exists`` branch failing to
    spend), which yields 2.

    The dates are asserted too, not just the count: a budget that stops the
    walk one occurrence early and a budget that starts it one late both give 2
    or 3 rows for the wrong reason. Only the exact grid
    ``{p_start, p_start+1mo, p_start+2mo}`` pins the right ones.

    Wrong implementations killed:
      * no series budget anywhere in generation (neither the query filter nor
        the in-loop guard) -- 6 rows;
      * ``occurrences_elapsed`` incremented before the create instead of with
        the frontier -- 2 rows;
      * ``active_series_filter`` missing from ``generate_due_transactions`` and
        the in-loop guard doing all the work, or vice versa -- both give 3
        here, which is why F-A and F-D fence the two arms separately.

    ⚠ **``remaining >= 0`` for ``> 0`` does NOT give 4 rows here**, and an
    earlier version of this list said it did. This template is MONTHLY, so the
    catch-up loop holds one occurrence per window and the SQL
    ``active_series_filter`` (``elapsed < count``, unmutated) drops the
    exhausted template at query time before the in-loop guard is ever
    consulted -- the row count and the date list are both unchanged. The
    mutant IS killed here, but only by the trailing
    ``has_remaining_occurrences(t) is False``, a direct predicate check. Its
    BEHAVIOURAL kill is F-A (``forecast_net`` -40.00 for -30) and F-H
    (``recurring_expense`` 40.00 for 30), which are WEEKLY and put several
    occurrences inside one window.
    """
    today = datetime.date.today()
    p_start = _safe_month_anchor(today - datetime.timedelta(days=5))
    seed = await _seed(db_session, p_start=p_start)
    count = 3

    t = _template(seed, next_due_date=p_start, occurrence_count=count)
    db_session.add(t)
    await db_session.commit()
    template_id = t.id

    cycle_start = p_start
    for _k in range(6):
        clock = cycle_start + datetime.timedelta(days=5)
        window_end = _assert_geometry(seed, clock, cycle_start)
        await recurring_service.generate_due_transactions(
            db_session, seed["org_id"], today=clock
        )
        cycle_start = await _roll_cycle(
            db_session, seed["org_id"], cycle_start, window_end
        )

    rows = await _rows(db_session, seed["org_id"])
    assert [r.date for r in rows] == [
        p_start,
        p_start + relativedelta(months=1),
        p_start + relativedelta(months=2),
    ]
    assert len(rows) == count
    t = await _reload(db_session, template_id)
    assert t.occurrences_elapsed == count
    assert remaining_occurrences(t) == 0
    assert has_remaining_occurrences(t) is False


# ─────────────────────────────────────────────────────────────────────────────
# F-D — fence. Exhaustion is DERIVED. ``is_active`` is NOT written.
# ─────────────────────────────────────────────────────────────────────────────

async def test_fd_exhaustion_never_writes_is_active_false(db_session):
    """FENCE. Flipping ``is_active`` on exhaustion is the obvious shortcut and
    it corrupts the resume path.

    ``is_active`` is USER intent ("I paused this"). Exhaustion is arithmetic.
    Collapsing the second onto the first is not merely a naming choice: an
    exhausted template that reads ``is_active = False`` makes the NEXT
    ``PUT {is_active: true}`` look like a REACTIVATION, so
    ``_reanchor_frontier_on_resume`` fires and drags the finished series'
    frontier forward onto the current billing cycle -- silently rewriting the
    schedule of a plan that is already over.

    The fixture rolls FIVE cycles for a 3-instalment plan, so at the moment of
    the ``PUT`` the frontier (``p_start + 3 months``) sits two cycles BEHIND
    ``p_start``. That gap is what makes the re-anchor observable: with the
    frontier already current the mutant's re-anchor would walk zero steps and
    the fence would be vacuous.

    Wrong implementations killed:
      * exhaustion sets ``is_active = False`` -- the first assertion fails
        outright, and the ``PUT`` then moves ``next_due_date`` forward by two
        months;
      * exhaustion deletes the row -- ``_reload`` raises.
    """
    today = datetime.date.today()
    p_start = _safe_month_anchor(today - datetime.timedelta(days=5))
    seed = await _seed(db_session, p_start=p_start)
    count = 3

    t = _template(seed, next_due_date=p_start, occurrence_count=count)
    db_session.add(t)
    await db_session.commit()
    template_id = t.id

    cycle_start = p_start
    clock = today
    for _k in range(5):
        clock = cycle_start + datetime.timedelta(days=5)
        window_end = _assert_geometry(seed, clock, cycle_start)
        await recurring_service.generate_due_transactions(
            db_session, seed["org_id"], today=clock
        )
        cycle_start = await _roll_cycle(
            db_session, seed["org_id"], cycle_start, window_end
        )

    t = await _reload(db_session, template_id)
    frontier_before = t.next_due_date
    rows_before = len(await _rows(db_session, seed["org_id"]))
    assert rows_before == count
    assert t.occurrences_elapsed == count

    # ⭐ THE assertion. Exhausted, and still active.
    assert t.is_active is True
    assert remaining_occurrences(t) == 0

    # Precondition for the re-anchor half: the frontier really is stale, so a
    # spurious reactivation would visibly move it.
    assert frontier_before < cycle_start

    resumed = await recurring_service.update_recurring(
        db_session, seed["org_id"], template_id,
        RecurringUpdate(is_active=True),
        today=cycle_start + datetime.timedelta(days=5),
    )
    assert resumed.next_due_date == frontier_before
    assert len(await _rows(db_session, seed["org_id"])) == rows_before

    # ...and it still generates nothing, in the cycle it was "resumed" into.
    await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=cycle_start + datetime.timedelta(days=5)
    )
    assert len(await _rows(db_session, seed["org_id"])) == rows_before


# ─────────────────────────────────────────────────────────────────────────────
# F-E — fence. Exhaustion never calls ``stop_recurring``.
# ─────────────────────────────────────────────────────────────────────────────

async def test_fe_exhaustion_never_calls_stop_recurring(db_session):
    """FENCE. ``stop_recurring`` on exhaustion destroys the grouping this
    feature exists to create.

    ``stop_recurring`` NULLs ``recurring_id`` on EVERY surviving transaction
    the template produced. Calling it when a series finishes would mean a
    completed 3-instalment plan loses the link joining its 3 rows at the exact
    moment the 3rd lands -- the user gets an instalment plan that erases itself
    on completion. It would also delete any pending future row it had already
    materialised.

    ``occurrences_elapsed`` being STORED rather than counted is what survives
    this class of damage generally, which is why it is not named
    ``occurrences_generated``; here we simply require that the damage never
    happens.

    Wrong implementations killed:
      * exhaustion calls ``stop_recurring`` -- all three rows come back with
        ``recurring_id IS NULL`` and ``is_active`` is False;
      * exhaustion calls ``delete_recurring`` -- the FK's ON DELETE SET NULL
        does the same thing to ``recurring_id``, and ``_reload`` raises.
    """
    today = datetime.date.today()
    p_start = _safe_month_anchor(today - datetime.timedelta(days=5))
    seed = await _seed(db_session, p_start=p_start)
    count = 3

    t = _template(seed, next_due_date=p_start, occurrence_count=count)
    db_session.add(t)
    await db_session.commit()
    template_id = t.id

    cycle_start = p_start
    for _k in range(count):
        clock = cycle_start + datetime.timedelta(days=5)
        window_end = _assert_geometry(seed, clock, cycle_start)
        await recurring_service.generate_due_transactions(
            db_session, seed["org_id"], today=clock
        )
        cycle_start = await _roll_cycle(
            db_session, seed["org_id"], cycle_start, window_end
        )

    t = await _reload(db_session, template_id)
    assert t.occurrences_elapsed == count

    rows = await _rows(db_session, seed["org_id"])
    assert len(rows) == count
    # ⭐ Every instalment still belongs to its series.
    assert [r.recurring_id for r in rows] == [template_id] * count
    assert t.is_active is True


# ─────────────────────────────────────────────────────────────────────────────
# F-F — fence. A counted series NEVER forfeits instalments to a pause.
#       ⚠ TWO CLOCKS. A single-clock fixture is vacuous (TBD-300).
# ─────────────────────────────────────────────────────────────────────────────

async def test_ff_resume_reanchors_frontier_without_spending_instalments(db_session):
    """FENCE. The frontier says WHERE; the counter says HOW MANY. Independent.

    Generate instalment 1 of 3, stop the template, let TWO cycles pass, resume
    it, then run to completion. The resume re-anchors the frontier onto the
    current cycle (TBD-300) -- and that must cost the user NOTHING. The series
    still delivers all 3 instalments; they simply land later.

    ⚠ **Two clocks, and the gap must be at least one cycle.** ``today_1``
    generates the first instalment; ``today_2``, two cycles later, resumes. A
    single-clock fixture leaves the frontier already current, so
    ``_reanchor_frontier_on_resume`` walks ZERO steps and a counter-spending
    mutant is indistinguishable from a correct one. TBD-300 shipped exactly
    that mistake and all five of its fences passed against unmodified ``main``.
    The zero-step degeneracy is ruled out explicitly below.

    Wrong implementations killed:
      * ``_reanchor_frontier_on_resume`` advancing ``occurrences_elapsed``
        alongside ``next_due_date`` (the symmetric-looking change) -- the
        counter reaches 3 during the resume, the series is exhausted before it
        has delivered anything else, and the run ends with 1 row not 3;
      * the resume advancing the counter by one -- 2 rows;
      * ``occurrences_elapsed`` reset on resume -- 4 rows.
    """
    today = datetime.date.today()
    p_start = _safe_month_anchor(today - datetime.timedelta(days=5))
    seed = await _seed(db_session, p_start=p_start)
    count = 3

    t = _template(seed, next_due_date=p_start, occurrence_count=count)
    db_session.add(t)
    await db_session.commit()
    template_id = t.id

    # ── CLOCK 1: deliver instalment 1 of 3.
    clock_1 = p_start + datetime.timedelta(days=5)
    window_end = _assert_geometry(seed, clock_1, p_start)
    await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=clock_1
    )
    t = await _reload(db_session, template_id)
    assert t.occurrences_elapsed == 1
    assert len(await _rows(db_session, seed["org_id"])) == 1

    await recurring_service.stop_recurring(db_session, seed["org_id"], template_id)
    t = await _reload(db_session, template_id)
    assert t.is_active is False
    frozen_frontier = t.next_due_date
    assert t.occurrences_elapsed == 1

    # ── Two cycles pass with the template stopped.
    cycle_start = p_start
    for _k in range(2):
        cycle_start = await _roll_cycle(
            db_session, seed["org_id"], cycle_start, window_end
        )
        window_end = _assert_geometry(
            seed, cycle_start + datetime.timedelta(days=5), cycle_start
        )

    # ── CLOCK 2, two cycles later. Resume.
    clock_2 = cycle_start + datetime.timedelta(days=5)
    assert clock_2 > clock_1
    # ⚠ Anti-degeneracy: the re-anchor must have real work to do, otherwise a
    # counter-spending mutant is invisible.
    assert frozen_frontier < cycle_start

    resumed = await recurring_service.update_recurring(
        db_session, seed["org_id"], template_id,
        RecurringUpdate(is_active=True), today=clock_2,
    )
    # The frontier moved (TBD-300) ...
    assert resumed.next_due_date == cycle_start
    assert resumed.next_due_date > frozen_frontier
    # ... and the counter did NOT. ⭐
    assert resumed.occurrences_elapsed == 1

    # ── Run to completion. The two remaining instalments are DELIVERED, late.
    for _k in range(4):
        clock = cycle_start + datetime.timedelta(days=5)
        window_end = _assert_geometry(seed, clock, cycle_start)
        await recurring_service.generate_due_transactions(
            db_session, seed["org_id"], today=clock
        )
        cycle_start = await _roll_cycle(
            db_session, seed["org_id"], cycle_start, window_end
        )

    rows = await _rows(db_session, seed["org_id"])
    assert len(rows) == count, [str(r.date) for r in rows]
    t = await _reload(db_session, template_id)
    assert t.occurrences_elapsed == count


# ─────────────────────────────────────────────────────────────────────────────
# F-G — fence. A SIXTH read site cannot be added without the filter.
# ─────────────────────────────────────────────────────────────────────────────

_GATED_MODULES = (
    forecast_service,
    recurring_service,
    forecast_plan_service,
    scenario_engine,
    recurring_generation,
)

# Functions that read ``RecurringTransaction`` WITHOUT the series filter, on
# purpose. Pinned by name so adding a sixth unfiltered read site is a
# deliberate edit to this set, reviewed, rather than an omission.
#
#   list_recurring   -- the CRUD listing. An exhausted instalment plan MUST
#                       stay visible: it is the user's record that the plan
#                       completed, and hiding it would look like data loss.
#                       It projects nothing and materialises nothing.
#
# Single-template lookups (``WHERE RecurringTransaction.id == ...``) are exempt
# structurally rather than by name -- see ``_selects_single_template``.
_UNFILTERED_READ_SITES = frozenset({"list_recurring"})

_FILTER_NAME = "active_series_filter"
_MODEL_NAME = "RecurringTransaction"


class _WhereSiteCollector(ast.NodeVisitor):
    """``(function name, where-call)`` for every ``select(RecurringTransaction…)
    .where(…)`` chain, attributed to its INNERMOST enclosing function."""

    def __init__(self) -> None:
        self.stack: list[str] = []
        self.sites: list[tuple[str, ast.Call]] = []

    def _visit_func(self, node) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "where"
            and _is_recurring_select(node.func.value)
        ):
            self.sites.append((self.stack[-1] if self.stack else "<module>", node))
        self.generic_visit(node)


def _is_recurring_select(node: ast.expr) -> bool:
    """True when ``node`` is (or wraps) ``select(RecurringTransaction…)``.

    Walks through intermediate chained calls (``.options(...)``) so
    ``select(X).options(...).where(...)`` is still recognised.
    """
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        node = node.func.value
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
        return False
    if node.func.id != "select":
        return False
    for arg in node.args:
        for sub in ast.walk(arg):
            if isinstance(sub, ast.Name) and sub.id == _MODEL_NAME:
                return True
    return False


def _names_in(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            out.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            out.add(sub.attr)
    return out


def _selects_single_template(call: ast.Call) -> bool:
    """True when the where-clause constrains ``RecurringTransaction.id``.

    A single-template CRUD lookup (update / stop / delete / re-read) must NOT
    carry the series filter: it operates on one known row by primary key and
    filtering an exhausted one out would 404 the user's own template.
    """
    for sub in ast.walk(call):
        if (
            isinstance(sub, ast.Attribute)
            and sub.attr == "id"
            and isinstance(sub.value, ast.Name)
            and sub.value.id == _MODEL_NAME
        ):
            return True
    return False


def test_fg_every_recurring_read_site_carries_the_series_filter():
    """FENCE. Structural guard over the five gate modules' ``select().where()``
    chains.

    The five modules listed in ``_GATED_MODULES`` read
    ``RecurringTransaction`` to project or materialise occurrences, and all
    five must agree about exhaustion. Nothing in CI can see one of them
    reverting to a bare ``is_active`` predicate -- there is no type checker,
    and such a site is green in every functional test that does not happen to
    exercise it. So the guard is over the SOURCE.

    ⚠ A value/behaviour test cannot replace this. The failure mode is a site
    that does not exist yet, and the whole point is to fail the moment somebody
    writes one.

    ⚠ **SCOPE, stated narrowly on purpose.** This guard sees exactly one
    shape: a ``.where(...)`` whose receiver chain bottoms out in
    ``select(<... RecurringTransaction ...>)``, inside one of the five modules
    named in ``_GATED_MODULES``. It is structurally BLIND to:

      * **join-shaped reads.** ``recurring_service._settle_due_auto`` selects
        ``Transaction`` and JOINs ``RecurringTransaction``;
        ``_is_recurring_select`` inspects only the ``select()`` args, so that
        site is invisible here. It is CORRECTLY ungated -- it promotes rows
        that generation already created, and filtering it would strand
        already-materialised pending rows of a finished plan forever -- but
        the exemption is a fact about the code, not something this test
        checked;
      * **every other module.** ``reports/sources/recurring.py`` reads
        templates and is not in ``_GATED_MODULES``; nothing here would notice
        it, or a sixth module added tomorrow. The set is a hand-maintained
        list, and widening it is a deliberate edit;
      * **ORM attribute traversal, ``db.get``, relationship loads, and raw
        SQL.** None of them are a ``select().where()`` chain.

    So the guarantee is: "no gate site in these five modules loses the filter
    from a select-where chain." It is NOT "every read of
    ``RecurringTransaction`` in the codebase is gated."

    Within that scope, every ``select(RecurringTransaction…).where(…)`` must
    either call ``active_series_filter()``, constrain
    ``RecurringTransaction.id`` (single-row CRUD), or be named in
    ``_UNFILTERED_READ_SITES`` -- a pinned set of one, with its reason recorded
    beside it.

    Wrong implementations killed:
      * any of the five gate sites reverting to a bare
        ``RecurringTransaction.is_active == True`` (verified: mutating
        ``build_world_state`` and ``generate_due_transactions`` each fail here
        by name);
      * a new unfiltered projection site added to any of the five modules.

    ⚠ Anti-vacuity: the site inventory itself is asserted non-empty and pinned
    to the five known projection/materialisation sites, so a refactor that
    renames ``.where`` chains out of existence fails here rather than silently
    checking nothing (``all(())`` is ``True``).
    """
    filtered: set[tuple[str, str]] = set()
    total_sites = 0

    for module in _GATED_MODULES:
        tree = ast.parse(inspect.getsource(module))
        collector = _WhereSiteCollector()
        collector.visit(tree)
        mod_name = module.__name__.rsplit(".", 1)[-1]

        assert collector.sites, f"no RecurringTransaction read sites found in {mod_name}"

        for func_name, call in collector.sites:
            total_sites += 1
            if _FILTER_NAME in _names_in(call):
                filtered.add((mod_name, func_name))
                continue
            if _selects_single_template(call):
                continue
            assert func_name in _UNFILTERED_READ_SITES, (
                f"{mod_name}.{func_name} reads RecurringTransaction without "
                f"{_FILTER_NAME}(). Every projection / materialisation site must "
                f"agree about series exhaustion; if this site genuinely must not "
                f"filter, add it to _UNFILTERED_READ_SITES with a reason."
            )

    # ⚠ The inventory, pinned. `all(())` is True and so is "we checked nothing".
    assert filtered == {
        ("forecast_service", "compute_forecast"),
        ("recurring_service", "generate_due_transactions"),
        ("forecast_plan_service", "populate_from_sources"),
        ("scenario_engine", "build_world_state"),
        ("recurring_generation", "is_due"),
    }, filtered
    assert total_sites >= len(filtered)


def test_fg_forecast_plan_service_has_no_hand_rolled_occurrence_walk():
    """FENCE. The duplicate grid walk is GONE, not merely budgeted.

    ``populate_from_sources`` used to hand-roll ``occurrences_in_window`` --
    its own fast-forward, its own collect loop, subtly different loop
    conditions and no iteration cap. A second copy of the occurrence grid is a
    second thing to remember to budget, and TBD-275 is precisely the kind of
    change that misses one. It now calls the shared helper.

    Wrong implementations killed:
      * the hand-rolled walk kept and budgeted in place -- the duplicate
        survives and the next occurrence-grid change has two places to touch
        again;
      * the walk kept and NOT budgeted -- functionally caught by F-J, but this
        catches it structurally, at the point of reintroduction.
    """
    src = inspect.getsource(forecast_plan_service)
    tree = ast.parse(src)
    called = {
        n.func.id for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "occurrences_in_window" in called
    assert "advance_date" not in called, (
        "forecast_plan_service must not walk the occurrence grid itself; "
        "occurrences_in_window is the one walk (TBD-275)"
    )


# ─────────────────────────────────────────────────────────────────────────────
# F-H — fence. The CREDIT-CARD path. Not optional.
# ─────────────────────────────────────────────────────────────────────────────

async def test_fh_credit_card_series_conserves_and_stops(db_session):
    """FENCE. F-A + F-C on a ``credit_card`` account.

    Instalment plans are overwhelmingly a CREDIT-CARD product ("12 x 49.00 on
    the Visa"), so the CC account path is the one users will actually hit, and
    this is the WEEKLY conservation + row-count pair run down it.

    ⚠ **Be honest about what "the CC path" means here.** An earlier version of
    this docstring justified the test by the card's "liability sign" and "its
    own forecast contributions". Neither is exercised: ``apply_balance`` is
    account-type-agnostic (``income`` adds, everything else subtracts, with no
    ``AccountType`` read anywhere), and ``compute_forecast`` contains ZERO
    account-type references. Structurally this test is F-A with a different
    FK, and it is worth keeping for exactly that reason -- it pins that the
    budget does not acquire an account-type dependence -- but it does not
    reach the credit-card MACHINERY. The fence that does is
    ``test_fh_cc_projected_payment_plateaus_when_the_series_exhausts`` at the
    bottom of this file, which drives
    ``account_balance_forecast_service`` + ``cc_forecast_service`` end to end.

    Both halves are asserted on the CC account:
      * conservation of ``forecast_net`` across a generation run (F-A), with
        the budget provably smaller than the window;
      * exactly ``count`` rows after six cycles (F-C).

    Wrong implementations killed:
      * a budget or filter made conditional on account type;
      * ``remaining >= 0`` for ``> 0`` -- ``recurring_expense`` reads 40.00
        for 30 (this is the weekly geometry F-C's monthly one cannot see);
      * the CC account's rows escaping the ``recurring_id`` grouping.
    """
    today = datetime.date.today()
    p_start = _safe_month_anchor(today - datetime.timedelta(days=5))
    seed = await _seed(db_session, p_start=p_start)
    count = 3

    t = _template(
        seed, account_id=seed["cc_account_id"], frequency="weekly",
        next_due_date=p_start, occurrence_count=count, auto_settle=True,
    )
    db_session.add(t)
    await db_session.commit()
    template_id = t.id

    window_end = _assert_geometry(seed, today, p_start)
    assert len(occurrences_in_window(
        p_start, Frequency.WEEKLY, p_start, window_end
    )) >= _WEEKLY_FLOOR > count

    before = await forecast_service.compute_forecast(
        db_session, seed["org_id"], today=today
    )
    assert Decimal(before["recurring_expense"]) == Decimal("30")
    await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=today
    )
    after = await forecast_service.compute_forecast(
        db_session, seed["org_id"], today=today
    )
    assert Decimal(after["forecast_net"]) == Decimal(before["forecast_net"])
    assert Decimal(after["forecast_net"]) == Decimal("-30")

    # F-C half: six more cycles change nothing.
    cycle_start = p_start
    for _k in range(6):
        clock = cycle_start + datetime.timedelta(days=5)
        window_end = _assert_geometry(seed, clock, cycle_start)
        await recurring_service.generate_due_transactions(
            db_session, seed["org_id"], today=clock
        )
        cycle_start = await _roll_cycle(
            db_session, seed["org_id"], cycle_start, window_end
        )

    rows = await _rows(db_session, seed["org_id"])
    assert len(rows) == count
    assert {r.account_id for r in rows} == {seed["cc_account_id"]}
    assert [r.recurring_id for r in rows] == [template_id] * count
    assert (await _reload(db_session, template_id)).occurrences_elapsed == count


# ─────────────────────────────────────────────────────────────────────────────
# F-I — fence. ``> 0``, never ``!= 0``. A downward count edit must STOP.
# ─────────────────────────────────────────────────────────────────────────────

def test_fi_remaining_is_not_clamped_and_guards_test_greater_than_zero():
    """FENCE. ``!= 0`` for ``> 0``, and the clamp that would hide it.

    ``remaining_occurrences`` must return the RAW difference, negatives
    included. Clamping to zero looks harmless and is not: it makes ``> 0`` and
    ``!= 0`` behave identically at every input, so the difference between a
    guard that stops and a guard that never fires again becomes UNOBSERVABLE by
    any test. The clamp does not fix the bug, it hides it.

    With a negative remaining and a ``!= 0`` guard, every step takes the value
    further from zero and the guard never fires: the series over-generates
    until some unrelated iteration cap happens to stop it.

    Wrong implementations killed:
      * ``return max(0, count - elapsed)`` -- the -1 assertion fails;
      * ``remaining != 0`` for ``remaining > 0`` in
        ``occurrences_in_window`` -- the negative and zero budgets both return
        the full occurrence list instead of ``[]``;
      * ``rem != 0`` for ``rem > 0`` in ``has_remaining_occurrences``.
    """
    over = RecurringTransaction(
        occurrence_count=2, occurrences_elapsed=3, frequency=Frequency.MONTHLY
    )
    # ⭐ NOT clamped.
    assert remaining_occurrences(over) == -1
    assert has_remaining_occurrences(over) is False

    exact = RecurringTransaction(occurrence_count=3, occurrences_elapsed=3)
    assert remaining_occurrences(exact) == 0
    assert has_remaining_occurrences(exact) is False

    live = RecurringTransaction(occurrence_count=3, occurrences_elapsed=1)
    assert remaining_occurrences(live) == 2
    assert has_remaining_occurrences(live) is True

    open_ended = RecurringTransaction(occurrence_count=None, occurrences_elapsed=7)
    assert remaining_occurrences(open_ended) is None
    assert has_remaining_occurrences(open_ended) is True

    # The walker's own guard, at the two non-positive budgets. Both loops are
    # exercised: the frontier is before ``start`` so the fast-forward runs too.
    start = datetime.date(2026, 3, 2)
    end = datetime.date(2026, 4, 30)
    frontier = datetime.date(2026, 2, 2)
    unbudgeted = occurrences_in_window(frontier, Frequency.WEEKLY, start, end)
    assert len(unbudgeted) >= _WEEKLY_FLOOR      # anti-vacuity: there IS a list to suppress
    assert occurrences_in_window(
        frontier, Frequency.WEEKLY, start, end, budget=0
    ) == []
    assert occurrences_in_window(
        frontier, Frequency.WEEKLY, start, end, budget=-1
    ) == []


async def test_fi_downward_count_edit_stops_the_series(db_session):
    """FENCE. Editing ``occurrence_count`` 5 -> 2 with 3 already elapsed must
    COMPLETE, not run on.

    The over-elapsed state is reachable in one ``PUT``: the user shortens a
    plan they have already over-run. ``remaining_occurrences`` goes negative,
    and everything downstream must read that as "stop", not as "budget
    available".

    Wrong implementations killed:
      * ``update_recurring`` rejecting a downward edit -- ``ValidationError``
        out of the ``PUT``, and the user's only escape from a runaway plan
        would be ``stop_recurring``, which destroys the ``recurring_id``
        grouping. This is the ONE mutant this test kills on its own.

    NOT killed here, recorded rather than claimed (both were listed above
    until they were injected and survived):
      * ``!= 0`` guards anywhere in the chain -- the fixture's ``remaining`` is
        -1, but the SQL ``active_series_filter`` (``elapsed < count``, 3 < 2)
        drops the template at query time, so neither the in-loop guard nor
        ``occurrences_in_window``'s budget is ever consulted and the row count
        stays 3. The unit fence above,
        ``test_fi_remaining_is_not_clamped_and_guards_test_greater_than_zero``,
        is the only thing that kills it;
      * ``elapsed < count`` written as ``<=`` in ``active_series_filter`` --
        no 4th row appears here (3 <= 2 is False either way, the template is
        dropped either way). It is killed by
        ``test_fj_scheduler_is_due_stops_reporting_work_when_exhausted``
        (elapsed 1, count 1: 1 <= 1 keeps ``is_due`` True) and by the
        scenario-engine fence.
    """
    today = datetime.date.today()
    p_start = _safe_month_anchor(today - datetime.timedelta(days=5))
    seed = await _seed(db_session, p_start=p_start)

    t = _template(seed, next_due_date=p_start, occurrence_count=5)
    db_session.add(t)
    await db_session.commit()
    template_id = t.id

    cycle_start = p_start
    window_end = _assert_geometry(seed, p_start + datetime.timedelta(days=5), p_start)
    for _k in range(3):
        clock = cycle_start + datetime.timedelta(days=5)
        window_end = _assert_geometry(seed, clock, cycle_start)
        await recurring_service.generate_due_transactions(
            db_session, seed["org_id"], today=clock
        )
        cycle_start = await _roll_cycle(
            db_session, seed["org_id"], cycle_start, window_end
        )

    t = await _reload(db_session, template_id)
    assert t.occurrences_elapsed == 3
    assert remaining_occurrences(t) == 2      # still running, under the OLD count

    # ── The downward edit.
    updated = await recurring_service.update_recurring(
        db_session, seed["org_id"], template_id,
        RecurringUpdate(occurrence_count=2),
        today=cycle_start + datetime.timedelta(days=5),
    )
    assert updated.occurrence_count == 2
    assert updated.occurrences_elapsed == 3
    assert remaining_occurrences(updated) == -1

    # ── Six more cycles. Nothing more may be created.
    for _k in range(6):
        clock = cycle_start + datetime.timedelta(days=5)
        window_end = _assert_geometry(seed, clock, cycle_start)
        await recurring_service.generate_due_transactions(
            db_session, seed["org_id"], today=clock
        )
        fc = await forecast_service.compute_forecast(
            db_session, seed["org_id"], today=clock
        )
        assert Decimal(fc["recurring_expense"]) == Decimal("0")
        cycle_start = await _roll_cycle(
            db_session, seed["org_id"], cycle_start, window_end
        )

    rows = await _rows(db_session, seed["org_id"])
    assert len(rows) == 3
    assert (await _reload(db_session, template_id)).occurrences_elapsed == 3


# ─────────────────────────────────────────────────────────────────────────────
# F-J — fence. The other three gate sites: plan source, scenario engine,
#       scheduler ``is_due``.
# ─────────────────────────────────────────────────────────────────────────────

async def test_fj_forecast_plan_source_spends_the_series_budget(db_session):
    """FENCE. ``populate_from_sources`` is a projection too, budget included.

    The plan source seeds a user's forecast plan from their active templates.
    An exhausted series seeding a plan line forever is the same defect as the
    forecast projecting it forever, and the hand-rolled walk it used to run is
    exactly the kind of duplicate that gets missed.

    Two templates on two DISTINCT categories, so a leak from either is named
    by the failure rather than silently summed into one line.

    ⚠ **Honest scope.** At this site ``active_series_filter`` and the
    ``budget=`` argument are REDUNDANT for an already-exhausted series: a
    filtered-out template contributes nothing, and an unfiltered one gets
    ``budget=0``, walks zero occurrences and contributes nothing either. So
    this test kills the missing BUDGET, not the missing filter. The filter is
    load-bearing on its own only in ``is_due`` (no walk exists there to
    budget), which is why that arm has its own fence below, and structurally
    everywhere via F-G.

    Wrong implementations killed:
      * ``budget=`` missing on the ``occurrences_in_window`` call -- the weekly
        template seeds ``_WEEKLY_FLOOR`` x 10.00 (>= 40.00) instead of 20.00;
      * the budget applied to the collect loop only -- same number here,
        because this fixture's frontier is IN the window; F-B is the fence for
        the fast-forward.
    """
    today = datetime.date.today()
    p_start = _safe_month_anchor(today - datetime.timedelta(days=5))
    seed = await _seed(db_session, p_start=p_start)
    window_end = _assert_geometry(seed, today, p_start)

    # (a) exhausted before the call -- must contribute nothing, on its OWN
    #     category so a leak is named rather than summed into (b)'s line.
    db_session.add(_template(
        seed, description="done", category_id=seed["cat_b"],
        amount=Decimal("500.00"), next_due_date=p_start,
        occurrence_count=2, occurrences_elapsed=2,
    ))
    # (b) exhausts DURING the walk -- must contribute exactly 2 x 10.
    db_session.add(_template(
        seed, description="running", frequency="weekly", amount=Decimal("10.00"),
        next_due_date=p_start, occurrence_count=2,
    ))
    await db_session.commit()

    assert len(occurrences_in_window(
        p_start, Frequency.WEEKLY, p_start, window_end
    )) >= _WEEKLY_FLOOR > 2

    plan = await forecast_plan_service.populate_from_sources(
        db_session, seed["org_id"], p_start, today=today
    )

    lines = [
        i for i in plan.items
        if i.type == "expense" and i.source == "recurring"
    ]
    # One line, for the running series only, and worth exactly its budget:
    # 2 x 10.00. Not 500.00 (the exhausted template), not _WEEKLY_FLOOR x 10.
    assert len(lines) == 1, [(i.category_id, str(i.planned_amount)) for i in lines]
    assert lines[0].category_id == seed["cat_id"]
    assert lines[0].planned_amount == Decimal("20.00")


async def test_fj_scenario_engine_stops_projecting_an_exhausted_series(db_session):
    """FENCE. The scenario horizon is YEARS. An unbudgeted series is ruinous.

    ``scenario_engine`` projects month by month over a multi-year horizon, far
    longer than any billing window, so a 12-instalment loan left unbudgeted
    keeps paying for the entire projection. Two arms again: exhausted before
    the call, and exhausting during the horizon walk.

    The budget rides in the projection QUEUE, not on the shared
    ``RecurringSnapshot``, so a second scenario projected from the same state
    starts with a full budget -- asserted here by projecting twice and
    requiring identical balances.

    Wrong implementations killed:
      * ``active_series_filter`` missing from ``build_world_state`` -- killed
        at ``assert len(state.recurring) == 1`` (``[1, 2]``), NOT by any
        balance assertion. The exhausted template does not "drain the account
        for the whole horizon", as an earlier version of this list claimed:
        its snapshot carries ``remaining = 0``, so the monthly walk breaks on
        pass 1 and the projected balance is unchanged. The membership
        assertion is the whole kill;
      * no budget in the monthly walk -- the running template posts every month
        instead of twice, and the final balance reads -200.00 for 800.00
        (which is what the anti-vacuity block at the end demonstrates
        directly);
      * the budget mutated on the snapshot -- the second projection posts
        nothing, the two runs disagree, and ``snap.remaining`` reads 0.
    """
    today = datetime.date.today()
    p_start = _safe_month_anchor(today - datetime.timedelta(days=5))
    seed = await _seed(db_session, p_start=p_start)

    db_session.add(_template(
        seed, description="done", category_id=seed["cat_b"],
        amount=Decimal("500.00"), next_due_date=p_start,
        occurrence_count=2, occurrences_elapsed=2,
    ))
    # ⚠ The frontier is placed INSIDE month 0 of the engine's horizon. The
    # engine anchors month 0 to the first of the current calendar month, which
    # is independent of the billing cycle: a frontier in the PREVIOUS calendar
    # month is legitimately consumed by the monthly walk's fast-forward (it is
    # a real occurrence generation will materialise), and the expected posting
    # count would then depend on where in the month the suite runs -- a date
    # bomb. Anchoring here removes that degree of freedom.
    horizon_start = scenario_engine._start_of_horizon()
    running_frontier = horizon_start + datetime.timedelta(days=5)
    db_session.add(_template(
        seed, description="running", amount=Decimal("100.00"),
        next_due_date=running_frontier, occurrence_count=2,
    ))
    await db_session.commit()

    state = await scenario_engine.build_world_state(
        db_session, org_id=seed["org_id"], user_id=1
    )
    # The exhausted one never even reaches the engine (the FILTER's job).
    assert len(state.recurring) == 1, [s.id for s in state.recurring]
    snap = state.recurring[0]
    assert snap.remaining == 2
    assert snap.amount == Decimal("100.00")
    assert snap.next_due_date >= horizon_start

    scenario = Scenario(
        org_id=seed["org_id"], user_id=1, name="s",
        scenario_type=ScenarioType.CUSTOM,
        params_json={"scenario_type": "custom", "currency": "EUR", "events": []},
        horizon_months=12,
    )
    engine = AnalyticEngine()

    def _final_balance(st) -> Decimal:
        result = engine.simulate(
            SimulationRequest(
                scenario=scenario, state=st, horizon_months=12, options={}
            )
        )
        points = next(
            p for p in result["per_account_series"]
            if p["account_id"] == seed["account_id"]
        )["points"]
        return Decimal(points[-1]["projected_balance"])

    # 1000.00 starting, exactly TWO instalments of 100.00 over 12 months.
    assert _final_balance(state) == Decimal("800.00")

    # ⚠ The budget rides in the QUEUE, not on the shared snapshot: a second
    # projection from the SAME state must post the same two instalments. A
    # snapshot-mutating implementation returns 1000.00 here and leaves
    # ``snap.remaining`` at 0.
    assert _final_balance(state) == Decimal("800.00")
    assert snap.remaining == 2

    # ⚠ ANTI-VACUITY. The same series with no budget drains the account for the
    # whole horizon -- 12 postings, not 2. Without this the fence cannot tell
    # "the budget stopped it" from "the horizon did".
    snap.remaining = None
    assert _final_balance(state) == Decimal("-200.00")
    snap.remaining = 2


async def test_fj_scheduler_is_due_stops_reporting_work_when_exhausted(db_session):
    """FENCE. ``is_due`` must not report work forever.

    ``is_due`` has no occurrence walk, so it is easy to leave alone -- and that
    is the bug. An exhausted series keeps ``is_active = True`` and keeps a
    ``next_due_date <= period_end``, so a bare ``is_active`` predicate returns
    True on every tick for the life of the org: the job wakes every 900
    seconds, generates nothing, and logs a no-op run forever.

    Both states are asserted on the SAME template, before and after exhaustion,
    so the difference cannot come from anything else in the fixture.

    ⚠ **It must be WEEKLY.** A MONTHLY 1-instalment template leaves its
    frontier at ``p_start + 1 month``, which is already PAST ``period_end``, so
    ``next_due_date <= period_end`` alone turns ``is_due`` False and the fence
    passes against the unfiltered predicate -- vacuous. A weekly template's
    frontier lands at ``p_start + 7 days``, comfortably inside the window, so
    the series filter is the ONLY thing that can make ``is_due`` False. That
    precondition is asserted below rather than assumed.

    ⚠ This is the one site where ``active_series_filter`` is load-bearing on
    its own: there is no occurrence walk here to carry a budget.

    Wrong implementations killed:
      * ``RecurringTransaction.is_active == True`` left in place -- the final
        assertion fails, ``is_due`` still True on every tick forever;
      * ``active_series_filter`` applied but inverted -- the first fails.
    """
    today = datetime.date.today()
    p_start = _safe_month_anchor(today - datetime.timedelta(days=5))
    seed = await _seed(db_session, p_start=p_start)
    job = recurring_generation.RecurringGenerationJob()

    t = _template(
        seed, frequency="weekly", next_due_date=p_start, occurrence_count=1
    )
    db_session.add(t)
    await db_session.commit()
    template_id = t.id

    org = await db_session.scalar(
        select(Organization).where(Organization.id == seed["org_id"])
    )
    clock = p_start + datetime.timedelta(days=5)
    period_end = _assert_geometry(seed, clock, p_start)

    assert await job.is_due(db_session, org, clock) is True

    await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=clock
    )
    t = await _reload(db_session, template_id)
    assert t.occurrences_elapsed == 1
    assert t.is_active is True                      # still active ...
    # ⚠ ANTI-VACUITY. The date predicate alone still matches, so only the
    # series filter can flip the answer below.
    assert t.next_due_date == p_start + datetime.timedelta(days=7)
    assert t.next_due_date <= period_end

    # ⭐ ... and no longer due.
    assert await job.is_due(db_session, org, clock) is False


# ─────────────────────────────────────────────────────────────────────────────
# F-K — fence. PROMOTE seeds 1; direct create seeds 0.
# ─────────────────────────────────────────────────────────────────────────────

async def test_fk_promote_seeds_one_elapsed_and_create_seeds_zero(db_session):
    """FENCE. The source transaction IS instalment 1.

    ``POST /transactions/{id}/promote-to-recurring`` is how an instalment plan
    is normally born: the user has just paid the first of twelve and turns it
    into a series. The source row already exists and is linked to the new
    template by ``recurring_id``, so the series has ALREADY delivered one
    occurrence and ``next_due_date`` is instalment 2. Seeding 0 there delivers
    13 instalments for a 12-instalment plan -- an off-by-one that charges the
    user real money.

    The direct ``POST /recurring`` path is the opposite: nothing has been
    delivered, so 0 is correct. Both are asserted here because the fence is the
    CONTRAST -- either seed alone is defensible in isolation, and a single-path
    test would pass against an implementation that used one value everywhere.

    Wrong implementations killed:
      * promote seeding 0 -- the counted series delivers ``count + 1``;
      * create seeding 1 -- the counted series delivers ``count - 1``;
      * ``occurrence_count`` not threaded through ``PromoteToRecurringRequest``
        -- promote produces an open-ended series and the plan never ends.
    """
    today = datetime.date.today()
    p_start = _safe_month_anchor(today - datetime.timedelta(days=5))
    seed = await _seed(db_session, p_start=p_start)

    # ── Direct create: 0 elapsed.
    created = await recurring_service.create_recurring(
        db_session, seed["org_id"],
        RecurringCreate(
            account_id=seed["account_id"], category_id=seed["cat_id"],
            description="direct", amount=Decimal("10.00"), type="expense",
            frequency="monthly", next_due_date=today + DAY, occurrence_count=12,
        ),
    )
    assert created.occurrence_count == 12
    assert created.occurrences_elapsed == 0
    assert remaining_occurrences(created) == 12

    # ── Promote: 1 elapsed, because the source row is instalment 1.
    src = Transaction(
        org_id=seed["org_id"], account_id=seed["account_id"],
        category_id=seed["cat_id"], description="instalment 1",
        amount=Decimal("49.00"), type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED, date=today, settled_date=today,
    )
    db_session.add(src)
    await db_session.commit()

    promoted_tx = await transaction_service.promote_to_recurring(
        db_session, seed["org_id"], src.id,
        PromoteToRecurringRequest(
            frequency="monthly", next_due_date=today + relativedelta(months=1),
            occurrence_count=12,
        ),
    )
    template = await _reload(db_session, promoted_tx.recurring_id)
    assert template.occurrence_count == 12
    # ⭐ THE assertion. The source transaction is already instalment 1 of 12.
    assert template.occurrences_elapsed == 1
    assert remaining_occurrences(template) == 11
    assert promoted_tx.recurring_id == template.id


# ─────────────────────────────────────────────────────────────────────────────
# F-L — fence. Open-ended templates are untouched by any of this.
# ─────────────────────────────────────────────────────────────────────────────

async def test_fl_open_ended_series_is_unbounded(db_session):
    """FENCE. ``occurrence_count IS NULL`` must remain genuinely unbounded.

    Every template that existed before TBD-275 has a NULL count, and the
    migration deliberately backfills nothing. The SQL filter is the risk: in
    SQL ``x < NULL`` is NULL, not TRUE, so a bare
    ``occurrences_elapsed < occurrence_count`` without the ``IS NULL`` arm
    silently drops EVERY pre-existing template -- the forecast empties out and
    generation stops for the whole install base.

    Six cycles, six rows, counter climbing, nothing filtered.

    Wrong implementations killed:
      * ``active_series_filter`` without the ``occurrence_count IS NULL`` arm
        -- 0 rows, and ``recurring_expense`` 0 from the first call;
      * ``remaining_occurrences`` returning 0 rather than ``None`` for a NULL
        count -- the series never generates at all.
    """
    today = datetime.date.today()
    p_start = _safe_month_anchor(today - datetime.timedelta(days=5))
    seed = await _seed(db_session, p_start=p_start)

    t = _template(seed, next_due_date=p_start, occurrence_count=None)
    db_session.add(t)
    await db_session.commit()
    template_id = t.id

    assert remaining_occurrences(t) is None
    assert has_remaining_occurrences(t) is True

    fc = await forecast_service.compute_forecast(
        db_session, seed["org_id"], today=p_start + datetime.timedelta(days=5)
    )
    assert Decimal(fc["recurring_expense"]) == Decimal("10")

    cycle_start = p_start
    for _k in range(6):
        clock = cycle_start + datetime.timedelta(days=5)
        window_end = _assert_geometry(seed, clock, cycle_start)
        await recurring_service.generate_due_transactions(
            db_session, seed["org_id"], today=clock
        )
        cycle_start = await _roll_cycle(
            db_session, seed["org_id"], cycle_start, window_end
        )

    rows = await _rows(db_session, seed["org_id"])
    assert len(rows) == 6
    t = await _reload(db_session, template_id)
    assert t.occurrences_elapsed == 6
    assert t.occurrence_count is None


async def test_fl_exists_branch_spends_budget_too(db_session):
    """FENCE. The ``exists`` branch advances the frontier and MUST spend budget.

    ``generate_due_transactions`` has two ways to consume an occurrence: create
    a row, or find one already there and skip. Both advance ``next_due_date``.
    If only the create branch spends the budget, a series whose occurrences
    were materialised by some other path delivers MORE than its declared count
    -- and, worse, the projection (which spends per occurrence, not per row)
    and generation disagree, so ``forecast_net`` moves.

    This is also why the counter is ``occurrences_elapsed`` and NOT
    ``occurrences_generated``: it is deliberately not equal to
    ``COUNT(*) WHERE recurring_id = template.id``, and this test pins that
    inequality (2 elapsed, 1 row generated by the service).

    Wrong implementations killed:
      * ``r.next_due_date = advance_date(...)`` left inline in the ``exists``
        branch instead of ``_advance_frontier`` -- the counter reads 1 after
        both occurrences are consumed, and a third row is created next cycle.
    """
    today = datetime.date.today()
    p_start = _safe_month_anchor(today - datetime.timedelta(days=5))
    seed = await _seed(db_session, p_start=p_start)

    t = _template(seed, frequency="weekly", next_due_date=p_start, occurrence_count=2)
    db_session.add(t)
    await db_session.flush()
    template_id = t.id

    # A row already sitting on the FIRST occurrence, not created by generation.
    db_session.add(Transaction(
        org_id=seed["org_id"], account_id=seed["account_id"],
        category_id=seed["cat_id"], description="pre-existing",
        amount=Decimal("10.00"), type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED, date=p_start, settled_date=p_start,
        recurring_id=template_id,
    ))
    await db_session.commit()

    clock = p_start + datetime.timedelta(days=5)
    _assert_geometry(seed, clock, p_start)
    res = await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=clock
    )

    # One occurrence was skipped (exists), one created. Both spent budget.
    assert res["generated"] == 1
    t = await _reload(db_session, template_id)
    assert t.occurrences_elapsed == 2          # ⭐ NOT 1
    assert has_remaining_occurrences(t) is False
    assert t.next_due_date == p_start + datetime.timedelta(days=14)

    rows = await _rows(db_session, seed["org_id"])
    assert [r.date for r in rows] == [p_start, p_start + datetime.timedelta(days=7)]


# ─────────────────────────────────────────────────────────────────────────────
# F-M — fence. PROMOTE with the frontier ON the source row's own date.
#       ⚠ The shape the Add-Transaction FAB sent for every default entry.
# ─────────────────────────────────────────────────────────────────────────────

async def test_fm_promote_with_frontier_on_source_date_delivers_the_full_count(
    db_session,
):
    """FENCE. ``occurrences_elapsed = 1`` UNCONDITIONALLY loses an instalment.

    F-K pins the ``next_due_date`` > ``tx.date`` shape, where the source row is
    instalment 1 and the frontier is instalment 2, so seeding 1 is right. This
    is the OTHER shape, and until this ticket it was the one the UI produced on
    every default entry: ``TransactionForm`` defaults the date to today and
    computed ``next_due_date = max(date, today)``, so a same-day entry promoted
    with the frontier ON the source row's own date. The FAB now advances by one
    period first, but the endpoint is public and the today-floor can still
    produce the equal case, so the backend owns this invariant either way.

    Generation's idempotency probe is ``recurring_id == r.id AND date == due``
    with NO status term and NO date lower bound, so at ``due == tx.date`` it
    MATCHES the source row, takes the ``exists`` branch and calls
    ``_advance_frontier`` -- spending an instalment for the source row. A
    template seeded with 1 has therefore spent TWO instalments for ONE real
    occurrence, and a 3-instalment plan materialises 2 rows while the UI reads
    "3 of 3".

    ⚠ **No conservation fence can see this.** Forecast and generation agree
    with each other perfectly: both walk the same grid with the same budget,
    and the budget is simply one short. ``forecast_net`` never moves. Only a
    fixture that counts the ROWS a promoted series ends up with can tell.

    ⚠ **WEEKLY, so all three occurrences fall inside one billing window.** The
    promote endpoint rejects a ``next_due_date`` before the WALL-CLOCK today
    (``PromoteToRecurringRequest._next_due_date_not_past`` plus the service
    guard), so the source row must be dated today and the grid must start
    there -- which rules out the ``_safe_month_anchor``'ed monthly grid every
    other fixture here uses. A weekly grid has no month-length path dependence
    at all, and the window provably holds all three (asserted below).

    Wrong implementations killed:
      * ``occurrences_elapsed=1`` unconditionally (this ticket as first
        committed) -- 2 transactions exist, not 3, and the template reads
        ``occurrences_elapsed == 3`` having created only one row;
      * ``occurrences_elapsed=0`` unconditionally -- NOT killed here, and it
        must not be: 0 IS this fixture's correct seed. F-K kills it, at
        ``assert template.occurrences_elapsed == 1`` (``0 == 1``). The two are
        a PAIR covering the two shapes of ``next_due_date`` vs ``tx.date``;
        neither one alone constrains the seed.
    """
    today = datetime.date.today()
    p_start = _safe_month_anchor(today - datetime.timedelta(days=5))
    seed = await _seed(db_session, p_start=p_start)
    count = 3
    grid = [today + datetime.timedelta(days=7 * k) for k in range(count)]

    window_end = _assert_geometry(seed, today, p_start)
    # Fixture precondition: the whole 3-occurrence weekly grid is inside ONE
    # generation window, so a single run settles the question.
    assert grid[-1] <= window_end
    assert grid[0] >= p_start

    src = Transaction(
        org_id=seed["org_id"], account_id=seed["account_id"],
        category_id=seed["cat_id"], description="instalment 1",
        amount=Decimal("10.00"), type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED, date=today, settled_date=today,
    )
    db_session.add(src)
    await db_session.commit()

    promoted_tx = await transaction_service.promote_to_recurring(
        db_session, seed["org_id"], src.id,
        PromoteToRecurringRequest(
            frequency="weekly",
            # ⭐ THE fixture property. Equal, not after -- what the FAB sends.
            next_due_date=today,
            occurrence_count=count,
        ),
    )
    template_id = promoted_tx.recurring_id
    template = await _reload(db_session, template_id)
    assert template.next_due_date == src.date
    # ⭐ The source row has NOT been counted yet; the ``exists`` branch will
    # count it, exactly once.
    assert template.occurrences_elapsed == 0
    assert remaining_occurrences(template) == count

    res = await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=today
    )
    # The source row is consumed by the ``exists`` branch, so generation
    # CREATES count - 1 and the series still ends up with ``count`` rows.
    assert res["generated"] == count - 1

    rows = await _rows(db_session, seed["org_id"])
    # ⭐ THE assertion. Three instalments declared, three rows delivered.
    assert len(rows) == count, [str(r.date) for r in rows]
    assert [r.date for r in rows] == grid
    assert [r.recurring_id for r in rows] == [template_id] * count

    template = await _reload(db_session, template_id)
    assert template.occurrences_elapsed == count
    assert has_remaining_occurrences(template) is False

    # ...and it stays at three across later cycles.
    cycle_start = p_start
    for _k in range(3):
        clock = cycle_start + datetime.timedelta(days=5)
        window_end = _assert_geometry(seed, clock, cycle_start)
        await recurring_service.generate_due_transactions(
            db_session, seed["org_id"], today=clock
        )
        cycle_start = await _roll_cycle(
            db_session, seed["org_id"], cycle_start, window_end
        )
    assert len(await _rows(db_session, seed["org_id"])) == count


# ─────────────────────────────────────────────────────────────────────────────
# F-N — fence. An UPWARD count edit is a RESUME. It must not back-fill.
# ─────────────────────────────────────────────────────────────────────────────

async def test_fn_upward_count_edit_reanchors_instead_of_backfilling(db_session):
    """FENCE. TBD-300's back-fill, re-entered through the door THIS ticket opens.

    An exhausted series keeps ``is_active = True`` by design, so raising
    ``occurrence_count`` never trips ``update_recurring``'s ``is_active``
    False->True transition -- but it un-excludes the template from
    ``active_series_filter`` after it has been excluded for months, and its
    frontier has been frozen for exactly as long. Generation's catch-up loop
    has no lower bound, so the raise hands it the whole finished stretch: with
    ``auto_settle`` on, every back-filled row is written SETTLED, dated inside
    a CLOSED billing period, and applied to the account balance.

    Frozen frontier + a filter that resumes is precisely TBD-300's causal
    chain with a different gate, and it takes the same re-anchor.

    ⚠ **The gap must be several cycles and the anti-degeneracy is asserted.**
    With the frontier already inside the current cycle the re-anchor walks
    ZERO steps and the fence cannot tell the implementations apart.

    ⚠ **``auto_settle`` is not decoration.** It is what turns "two extra
    pending rows" into "two settled rows inside closed periods that moved the
    balance", and the balance assertion below is what pins that.

    Wrong implementations killed:
      * no re-anchor on the un-exhaustion transition (this ticket as first
        committed) -- the two new rows land at ``frozen + 1mo`` and
        ``frozen + 2mo``, four and three cycles in the past, both SETTLED,
        both inside closed periods;
      * the re-anchor gated on ``body.occurrence_count is not None`` rather
        than on the transition -- NOT killed here (this series IS exhausted, so
        both spellings fire). The fence for that one is
        ``test_fn_count_edit_on_a_running_series_leaves_the_frontier_alone``,
        immediately below, and it had to be written: the field-gated mutant
        survives every other test in this file;
      * ``next_due_date = today`` instead of ``p_start`` -- the row lands on
        the resume day rather than the cycle's own day, and the date equality
        below fails.
    """
    today = datetime.date.today()
    p_start = _safe_month_anchor(today - datetime.timedelta(days=5))
    seed = await _seed(db_session, p_start=p_start)

    t = _template(
        seed, next_due_date=p_start, occurrence_count=2, auto_settle=True,
    )
    db_session.add(t)
    await db_session.commit()
    template_id = t.id

    acct = await db_session.get(Account, seed["account_id"])
    opening = Decimal(str(acct.balance))

    # ── Deliver both instalments, then let FOUR more cycles pass. Generation
    #    is run every cycle: the exhausted series must contribute nothing.
    cycle_start = p_start
    for _k in range(6):
        clock = cycle_start + datetime.timedelta(days=5)
        window_end = _assert_geometry(seed, clock, cycle_start)
        await recurring_service.generate_due_transactions(
            db_session, seed["org_id"], today=clock
        )
        cycle_start = await _roll_cycle(
            db_session, seed["org_id"], cycle_start, window_end
        )

    t = await _reload(db_session, template_id)
    frozen_frontier = t.next_due_date
    settled_before = await _rows(db_session, seed["org_id"])
    assert len(settled_before) == 2
    assert t.occurrences_elapsed == 2
    assert t.is_active is True                       # exhausted, still active
    assert has_remaining_occurrences(t) is False
    # ⚠ ANTI-DEGENERACY. The frontier really is several cycles stale, so a
    # missing re-anchor is visible rather than a zero-step no-op.
    assert frozen_frontier < cycle_start
    assert frozen_frontier + relativedelta(months=1) < cycle_start

    await db_session.refresh(acct)
    balance_before = Decimal(str(acct.balance))
    assert balance_before == opening - Decimal("20.00")

    # Periods that are ALREADY closed at the moment of the edit, snapshotted as
    # plain tuples: `_roll_cycle` below closes further periods, and the rows the
    # resumed series writes into the then-open cycle land inside one of THOSE
    # legitimately. Only back-filling into a period that was closed BEFORE the
    # edit is the defect.
    closed_at_edit = [
        (p.start_date, p.end_date)
        for p in (await db_session.execute(
            select(BillingPeriod).where(
                BillingPeriod.org_id == seed["org_id"],
                BillingPeriod.end_date.is_not(None),
            )
        )).scalars().all()
    ]
    assert closed_at_edit, "fixture never closed a period"

    # ── The upward edit. 2 -> 4.
    clock = cycle_start + datetime.timedelta(days=5)
    updated = await recurring_service.update_recurring(
        db_session, seed["org_id"], template_id,
        RecurringUpdate(occurrence_count=4), today=clock,
    )
    assert updated.occurrence_count == 4
    assert updated.occurrences_elapsed == 2
    # ⭐ THE assertion. The frontier is dragged onto the CURRENT cycle, exactly
    # as a resume does -- not left in the closed past.
    assert updated.next_due_date == cycle_start
    # ⚠ Snapshot it. ``updated`` is a LIVE ORM row and generation below advances
    # ``next_due_date`` on that same identity-mapped instance, so reading the
    # attribute after the loop yields the FINAL frontier, not the resume point.
    resume_cycle = cycle_start

    # ── Run the two new instalments out, one per cycle.
    for _k in range(2):
        clock = cycle_start + datetime.timedelta(days=5)
        window_end = _assert_geometry(seed, clock, cycle_start)
        await recurring_service.generate_due_transactions(
            db_session, seed["org_id"], today=clock
        )
        cycle_start = await _roll_cycle(
            db_session, seed["org_id"], cycle_start, window_end
        )

    rows = await _rows(db_session, seed["org_id"])
    assert len(rows) == 4, [str(r.date) for r in rows]
    new_rows = [r for r in rows if r.id not in {x.id for x in settled_before}]
    assert len(new_rows) == 2

    # ⭐ Not one of them is dated before the cycle the edit happened in.
    assert all(r.date >= resume_cycle for r in new_rows), (
        [str(r.date) for r in new_rows], str(resume_cycle)
    )
    # ...and none of them landed inside a period that was ALREADY closed when
    # the edit ran, which is the damage the date assertion above is a proxy for.
    for r in new_rows:
        assert r.status == TransactionStatus.SETTLED   # auto_settle really is on
        for p_from, p_to in closed_at_edit:
            assert not (p_from <= r.date <= p_to), (
                f"settled row {r.date} back-filled into closed period "
                f"{p_from}..{p_to}"
            )

    await db_session.refresh(acct)
    assert Decimal(str(acct.balance)) == opening - Decimal("40.00")
    assert (await _reload(db_session, template_id)).occurrences_elapsed == 4


async def test_fn_count_edit_on_a_running_series_leaves_the_frontier_alone(
    db_session,
):
    """FENCE. The re-anchor is gated on the TRANSITION, not on the field.

    ``if body.occurrence_count is not None: reanchor()`` looks like the same
    rule and is not. A series that still has instalments left has never been
    excluded from generation, so its frontier is wherever the schedule says --
    and moving it on a count edit silently RESCHEDULES a live plan. A user who
    corrects "12" to "14" on a template that is a couple of cycles overdue
    would lose the overdue occurrences: the frontier jumps to the current cycle
    and the catch-up loop has nothing left to catch up.

    ⚠ **The frontier must start STALE.** With it already inside the current
    cycle the field-gated mutant walks zero steps and is invisible -- which is
    exactly why every other test in this file passes against it. The staleness
    is asserted, not assumed.

    ⚠ Both directions are edited, on two separate templates: UP (12 -> 14) and
    DOWN (12 -> 8, still above ``occurrences_elapsed``). Both are edits to a
    RUNNING series and neither may move anything.

    Wrong implementations killed:
      * the re-anchor gated on ``body.occurrence_count is not None`` -- both
        frontiers jump to the current cycle start;
      * the re-anchor gated on ``body.occurrence_count is not None and
        occurrence_count > old`` -- the upward arm alone still jumps;
      * ``had_remaining`` captured AFTER the fields are applied -- it would
        then equal the post-edit value, ``unexhausted`` would be constantly
        False, and F-N above goes RED instead.
    """
    today = datetime.date.today()
    p_start = _safe_month_anchor(today - datetime.timedelta(days=5))
    seed = await _seed(db_session, p_start=p_start)

    # Two RUNNING counted templates whose frontiers are three cycles behind the
    # current one. Overdue is ordinary: generation only runs when the scheduler
    # ticks or the user hits /generate.
    stale = p_start - relativedelta(months=3)
    up = _template(
        seed, description="up", next_due_date=stale,
        occurrence_count=12, occurrences_elapsed=2,
    )
    down = _template(
        seed, description="down", category_id=seed["cat_b"], next_due_date=stale,
        occurrence_count=12, occurrences_elapsed=2,
    )
    db_session.add_all([up, down])
    await db_session.commit()

    # ⚠ ANTI-DEGENERACY. Stale, and still running -- the two properties that
    # make the mutant observable.
    assert stale < p_start
    for t in (up, down):
        assert has_remaining_occurrences(t) is True

    clock = p_start + datetime.timedelta(days=5)
    _assert_geometry(seed, clock, p_start)

    raised = await recurring_service.update_recurring(
        db_session, seed["org_id"], up.id,
        RecurringUpdate(occurrence_count=14), today=clock,
    )
    lowered = await recurring_service.update_recurring(
        db_session, seed["org_id"], down.id,
        RecurringUpdate(occurrence_count=8), today=clock,
    )

    # ⭐ THE assertions. Neither edit touched the schedule.
    assert raised.next_due_date == stale
    assert lowered.next_due_date == stale
    assert raised.occurrence_count == 14
    assert lowered.occurrence_count == 8
    assert raised.occurrences_elapsed == 2
    assert lowered.occurrences_elapsed == 2


# ─────────────────────────────────────────────────────────────────────────────
# F-H2 — fence. The CC *balance forecast*, end to end. Correctness here is
#        TRANSITIVE, and this is what pins the transitivity.
# ─────────────────────────────────────────────────────────────────────────────

async def test_fh_cc_projected_payment_plateaus_when_the_series_exhausts(
    db_session,
):
    """FENCE. The projected credit-card payment PLATEAUS. It must not grow.

    ``cc_forecast_service`` and ``account_balance_forecast_service`` contain
    zero ``RecurringTransaction`` references, so nothing in this ticket touched
    them and F-G cannot see them. Their correctness under an instalment plan is
    therefore TRANSITIVE: generation stops writing rows, the CC ledger stops
    seeing them, and the projected payment stops climbing. A transitive
    argument that is never executed is just an argument -- this runs it.

    A 3-instalment WEEKLY ``auto_settle`` series on a real credit card
    (``close_day`` + ``payment_day`` + ``payment_source_account_id``, so
    ``compute_account_balance_forecast`` actually synthesises a payment).
    Every instalment is a SETTLED charge on the card. After the third, the
    owed amount is frozen at 30.00 and every later cycle must project the same
    30.00 payment out of the checking account.

    ⚠ **Compare two LATE cycles, not the first two.** Cycle 0 is where the
    charges land, so its projection legitimately differs from cycle 1's. By
    cycles 2 and 3 every charge is long past every close date, and the only
    thing that can move the number is generation creating more.

    ⚠ **The two halves of the final assertion kill DIFFERENT mutants, and the
    difference was measured, not assumed:**

      * ``== Decimal("30.00")`` (the AMOUNT) kills the missing in-loop
        ``has_remaining_occurrences`` guard. The query filter still stops the
        series after the cycle it exhausts in, so the projection settles at a
        WRONG-BUT-STABLE 50.00 -- ``[0, 30.00, 50.00, 50.00]``. A pure plateau
        assertion is GREEN against it.
      * ``projected[-1] == projected[-2]`` (the PLATEAU) kills the case where
        both gates are gone: ``[0, 30.00, 80.00, 120.00]``, growing without
        bound. The amount alone would report that too, but the plateau is what
        names the failure as "still generating".

    Wrong implementations killed:
      * no in-loop ``has_remaining_occurrences`` guard in
        ``generate_due_transactions`` -- ``cc.balance`` reads -50.00 and the
        late-cycle projection reads 50.00;
      * that guard AND ``active_series_filter`` both missing -- the projection
        climbs 30 -> 80 -> 120 across cycles.

    NOT killed here, deliberately recorded rather than papered over:
      * ``active_series_filter`` alone missing from ``generate_due_transactions``
        (the in-loop guard still stops the walk at the right occurrence, so
        every number in this fixture is unchanged). F-G catches that one
        structurally, and it is the only thing that does.
    """
    today = datetime.date.today()
    p_start = _safe_month_anchor(today - datetime.timedelta(days=5))
    seed = await _seed(db_session, p_start=p_start)
    count = 3

    cc = await db_session.get(Account, seed["cc_account_id"])
    # A card that actually bills: closes on the 15th, pays on the 5th of the
    # following month, out of the checking account seeded above. Both EUR --
    # cross-currency is a documented no-op in the synthesiser.
    cc.close_day = 15
    cc.payment_day = 5
    cc.payment_day_relative_month = 1
    cc.payment_source_account_id = seed["account_id"]
    cc.payment_strategy = PaymentStrategy.FULL_BALANCE
    cc.opening_balance = Decimal("0.00")

    t = _template(
        seed, account_id=seed["cc_account_id"], frequency="weekly",
        next_due_date=p_start, occurrence_count=count, auto_settle=True,
    )
    db_session.add(t)
    await db_session.commit()

    window_end = _assert_geometry(seed, today, p_start)
    # Anti-vacuity: the window holds strictly more weekly occurrences than the
    # series has instalments, so the BUDGET is what stops the card's charges.
    assert len(occurrences_in_window(
        p_start, Frequency.WEEKLY, p_start, window_end
    )) >= _WEEKLY_FLOOR > count

    async def _projected_cc_payment(period_start: datetime.date,
                                    clock: datetime.date) -> Decimal:
        result = await compute_account_balance_forecast(
            db_session, seed["org_id"], period_start=period_start, today=clock
        )
        row = next(
            a for a in result["accounts"] if a["account_id"] == seed["cc_account_id"]
        )
        return sum(
            (Decimal(p["amount"]) for p in row["cc_payments"]), Decimal("0")
        )

    projected: list[Decimal] = []
    cycle_start = p_start
    for _k in range(4):
        clock = cycle_start + datetime.timedelta(days=5)
        window_end = _assert_geometry(seed, clock, cycle_start)
        await recurring_service.generate_due_transactions(
            db_session, seed["org_id"], today=clock
        )
        projected.append(await _projected_cc_payment(cycle_start, clock))
        cycle_start = await _roll_cycle(
            db_session, seed["org_id"], cycle_start, window_end
        )

    await db_session.refresh(cc)
    # The card took exactly three 10.00 charges and no more.
    assert Decimal(str(cc.balance)) == Decimal("-30.00")
    rows = await _rows(db_session, seed["org_id"])
    assert len(rows) == count
    assert {r.account_id for r in rows} == {seed["cc_account_id"]}

    # ⚠ ANTI-VACUITY. The synthesiser really did project something; a fence
    # over a list of zeros would pass against every mutant.
    assert projected[-1] > Decimal("0")
    # ⭐ THE assertion. Two late cycles, the same number. Not growing.
    assert projected[-1] == projected[-2] == Decimal("30.00"), projected
