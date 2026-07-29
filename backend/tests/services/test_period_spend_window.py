"""TBD-240 — the open period's spend window, at the three unbounded sites
plus the two snapshot readers.

Spec: ``specs/2026-07-28-open-period-spend-window-design.md`` §5, tests 8-17,
plus three D6 threading fences and one transfer-side stranded-fallback fence
added in review (§5 covers neither).

Two rules govern every test in this file.

**Public entry points only.** ``_compute_spent``, ``_compute_actuals_batch``
and ``_gather_facts`` all keep their signatures (§4) — the window derivation
moved to their *callers*. A test that calls one of them directly therefore
passes identically against ``main`` and fences nothing; that vacuity class was
caught three separate times in review on this one spec. So the forecast test
drives ``get_plan_for_period`` and the rebalance test drives
``suggest_rebalance``.

**Dates are anchored on ``date.today()``**, never on calendar literals
(``reference_wall_clock_date_bomb_tests``): the whole point of
``period_spend_window_end`` is that it floors at today, so a fixed literal near
that boundary is a date bomb by construction. The one test that patches the
clock outright (:func:`_scripted_clock`) still derives its script from
``TODAY``, so nothing here carries a hardcoded calendar date.
"""
from __future__ import annotations

import datetime
import types
from decimal import Decimal
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.account import Account, AccountType
from app.models.billing import BillingPeriod
from app.models.budget import Budget
from app.models.category import Category, CategoryType
from app.models.forecast_plan import (
    ForecastItemType,
    ForecastPlan,
    ForecastPlanItem,
    ItemSource,
)
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization
from app.schemas.budget import BudgetUpdate
from app.services import (
    billing_service,
    budget_rebalance_service,
    budget_service,
    forecast_plan_service,
)
from app.services.ai_dispatch import NoRoutingConfigured


TODAY = datetime.date.today()


def _d(offset: int) -> datetime.date:
    """A date `offset` days from today. Every fixture date goes through here."""
    return TODAY + datetime.timedelta(days=offset)


def _scripted_clock(script: list[datetime.date]):
    """A ``date`` subclass whose ``today()`` walks ``script``, plus its call log.

    Used to simulate a request that STRADDLES MIDNIGHT: the first wall-clock
    read lands on one day, the next on the following one. The returned list
    records every read, so a test can assert not just *which* date was used but
    *how many times* the clock was consulted.

    Subclasses ``datetime.date`` rather than faking it, for the reason
    ``test_billing_service.test_spend_window_respects_injected_today`` records:
    the service modules construct and compare ``date`` values, and SQLAlchemy's
    Date coercion is isinstance-based.
    """
    calls: list[datetime.date] = []

    class _ScriptedDate(datetime.date):
        @classmethod
        def today(cls) -> datetime.date:
            # Past the end of the script the clock simply stops advancing.
            value = script[min(len(calls), len(script) - 1)]
            calls.append(value)
            return value

    return _ScriptedDate, calls


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_base(factory) -> dict:
    """Org + account + two master expense categories."""
    org_id = 1
    async with factory() as db:
        db.add(Organization(id=org_id, name="org", billing_cycle_day=1))
        await db.commit()
        at = AccountType(org_id=org_id, name="Cash", slug="cash", is_system=True)
        db.add(at)
        await db.commit()
        acc = Account(
            org_id=org_id, account_type_id=at.id, name="Wallet",
            balance=Decimal("0"), currency="EUR",
        )
        db.add(acc)
        await db.commit()
        groceries = Category(
            org_id=org_id, name="Groceries", slug="groceries",
            type=CategoryType.EXPENSE,
        )
        dining = Category(
            org_id=org_id, name="Dining", slug="dining", type=CategoryType.EXPENSE,
        )
        db.add_all([groceries, dining])
        await db.commit()
        return {
            "org_id": org_id,
            "account_id": acc.id,
            "groceries_id": groceries.id,
            "dining_id": dining.id,
        }


async def _add_period(
    factory, org_id: int, start: datetime.date, end: datetime.date | None = None
) -> int:
    async with factory() as db:
        p = BillingPeriod(org_id=org_id, start_date=start, end_date=end)
        db.add(p)
        await db.commit()
        return p.id


async def _add_budget(
    factory, org_id: int, category_id: int, start: datetime.date,
    end: datetime.date | None = None, amount: str = "1000.00",
) -> int:
    async with factory() as db:
        b = Budget(
            org_id=org_id, category_id=category_id, amount=Decimal(amount),
            period_start=start, period_end=end,
        )
        db.add(b)
        await db.commit()
        return b.id


async def _add_expense(
    factory, seed: dict, category_id: int, amount: str, settled: datetime.date,
) -> None:
    """A SETTLED expense — the only population every in-scope query can see.

    Seeded directly rather than through ``generate_due_transactions``: that
    path produces PENDING rows unless ``auto_settle`` is set (it defaults
    False), and all three in-scope queries filter ``status == SETTLED``, so a
    recurring-driven fixture would assert 0 == 0 and fence nothing (§5 test 9).
    """
    async with factory() as db:
        db.add(Transaction(
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=category_id, description="x",
            amount=Decimal(amount), type=TransactionType.EXPENSE,
            status=TransactionStatus.SETTLED,
            date=settled, settled_date=settled,
        ))
        await db.commit()


async def _add_plan_with_item(
    factory, org_id: int, period_id: int, category_id: int, planned: str = "500.00",
) -> None:
    """Create the period's ``ForecastPlan`` **and** one item on it.

    Named for what it does. It is not an "add an item" helper and must not be
    called twice for the same period: the second call would insert a second
    plan row for a period that is meant to carry exactly one. Every test here
    wants one plan with one item, so the combined shape is the right one —
    split it if a test ever needs two items.
    """
    async with factory() as db:
        plan = ForecastPlan(org_id=org_id, billing_period_id=period_id)
        db.add(plan)
        await db.commit()
        db.add(ForecastPlanItem(
            plan_id=plan.id, org_id=org_id, category_id=category_id,
            type=ForecastItemType.EXPENSE, planned_amount=Decimal(planned),
            source=ItemSource.MANUAL,
        ))
        await db.commit()


# ── Test 8 — healthy roster: the future-dated row is dropped ────────────────

@pytest.mark.asyncio
async def test_list_budgets_excludes_rows_beyond_the_next_period_start(
    session_factory,
):
    """Healthy roster (open row, stub ahead): spend stops the day before the
    stub starts, so a transaction dated inside the stub's window no longer
    counts against the open period. Spend moves DOWN.
    """
    seed = await _seed_base(session_factory)
    org_id = seed["org_id"]
    await _add_period(session_factory, org_id, _d(-5), None)
    await _add_period(session_factory, org_id, _d(20), _d(49))
    await _add_budget(session_factory, org_id, seed["groceries_id"], _d(-5))
    await _add_expense(session_factory, seed, seed["groceries_id"], "100.00", _d(0))
    await _add_expense(session_factory, seed, seed["groceries_id"], "50.00", _d(25))

    async with session_factory() as db:
        rows = await budget_service.list_budgets(db, org_id, period_start=_d(-5))

    assert len(rows) == 1
    assert rows[0].spent == Decimal("100.00"), (
        "the row dated inside the NEXT period's window must not count against "
        "the open period"
    )
    assert rows[0].remaining == Decimal("900.00")


# ── Test 9 — the floor's regression fence ──────────────────────────────────

@pytest.mark.asyncio
async def test_list_budgets_still_counts_today_on_a_lapsed_roster(session_factory):
    """Lapsed roster: the derived end is in the PAST, so without the today
    floor the org's current spending would vanish from every editable view.

    The control below is the floor-removed comparison: there is no floor
    toggle, so the same fixture is re-run through ``_compute_spent`` with the
    *pure* derived end and must return 0. That is what makes the first
    assertion non-vacuous (§5 test 9).
    """
    seed = await _seed_base(session_factory)
    org_id = seed["org_id"]
    await _add_period(session_factory, org_id, _d(-95), None)
    await _add_period(session_factory, org_id, _d(-65), _d(-36))
    await _add_budget(session_factory, org_id, seed["groceries_id"], _d(-95))
    await _add_expense(session_factory, seed, seed["groceries_id"], "100.00", _d(0))

    async with session_factory() as db:
        rows = await budget_service.list_budgets(db, org_id, period_start=_d(-95))

    assert rows[0].spent == Decimal("100.00"), (
        "today's settled spend must stay visible in the open period even when "
        "the roster has lapsed"
    )

    async with session_factory() as db:
        period = (
            await db.execute(
                select(BillingPeriod).where(
                    BillingPeriod.org_id == org_id,
                    BillingPeriod.start_date == _d(-95),
                )
            )
        ).scalar_one()
        naive_end = await billing_service.period_effective_end(db, org_id, period)
        unfloored = await budget_service._compute_spent(
            db, org_id, seed["groceries_id"], _d(-95), naive_end,
        )
    assert naive_end == _d(-66)
    assert unfloored == Decimal("0"), (
        "control: the unfloored derived end hides today's spend entirely — "
        "this is the under-count the floor exists to prevent"
    )


# ── Test 10 — roster tail: byte-identical to pre-change ────────────────────

@pytest.mark.asyncio
async def test_list_budgets_roster_tail_is_unchanged(session_factory):
    """No later period on the roster → the window stays unbounded, exactly as
    before TBD-240. Fresh orgs and single-period orgs must not move.
    """
    seed = await _seed_base(session_factory)
    org_id = seed["org_id"]
    await _add_period(session_factory, org_id, _d(-5), None)
    await _add_budget(session_factory, org_id, seed["groceries_id"], _d(-5))
    await _add_expense(session_factory, seed, seed["groceries_id"], "100.00", _d(0))
    await _add_expense(session_factory, seed, seed["groceries_id"], "50.00", _d(25))

    async with session_factory() as db:
        rows = await budget_service.list_budgets(db, org_id, period_start=_d(-5))

    assert rows[0].spent == Decimal("150.00")


# ── Test 11 — forecast actuals, through the public getter ──────────────────

@pytest.mark.asyncio
async def test_forecast_actuals_healthy_roster_excludes_future_rows(
    session_factory,
):
    seed = await _seed_base(session_factory)
    org_id = seed["org_id"]
    period_id = await _add_period(session_factory, org_id, _d(-5), None)
    await _add_period(session_factory, org_id, _d(20), _d(49))
    await _add_plan_with_item(session_factory, org_id, period_id, seed["groceries_id"])
    await _add_expense(session_factory, seed, seed["groceries_id"], "100.00", _d(0))
    await _add_expense(session_factory, seed, seed["groceries_id"], "50.00", _d(25))

    async with session_factory() as db:
        plan = await forecast_plan_service.get_plan_for_period(
            db, org_id, period_start=_d(-5)
        )

    assert plan is not None
    assert plan.total_actual_expense == Decimal("100.00")
    assert plan.items[0].actual_amount == Decimal("100.00")
    # D5: the emitted `period_end` stays the RAW stored column, never the
    # derived window.
    assert plan.period_end is None


@pytest.mark.asyncio
async def test_forecast_actuals_lapsed_roster_still_counts_today(session_factory):
    seed = await _seed_base(session_factory)
    org_id = seed["org_id"]
    period_id = await _add_period(session_factory, org_id, _d(-95), None)
    await _add_period(session_factory, org_id, _d(-65), _d(-36))
    await _add_plan_with_item(session_factory, org_id, period_id, seed["groceries_id"])
    await _add_expense(session_factory, seed, seed["groceries_id"], "100.00", _d(0))
    await _add_expense(session_factory, seed, seed["groceries_id"], "70.00", _d(25))

    async with session_factory() as db:
        plan = await forecast_plan_service.get_plan_for_period(
            db, org_id, period_start=_d(-95)
        )

    assert plan is not None
    # Floored at today: today's row counts, the future row does not.
    assert plan.total_actual_expense == Decimal("100.00")


@pytest.mark.asyncio
async def test_forecast_actuals_roster_tail_is_unchanged(session_factory):
    seed = await _seed_base(session_factory)
    org_id = seed["org_id"]
    period_id = await _add_period(session_factory, org_id, _d(-5), None)
    await _add_plan_with_item(session_factory, org_id, period_id, seed["groceries_id"])
    await _add_expense(session_factory, seed, seed["groceries_id"], "100.00", _d(0))
    await _add_expense(session_factory, seed, seed["groceries_id"], "50.00", _d(25))

    async with session_factory() as db:
        plan = await forecast_plan_service.get_plan_for_period(
            db, org_id, period_start=_d(-5)
        )

    assert plan is not None
    assert plan.total_actual_expense == Decimal("150.00")


# ── Test 12 — AI rebalance, through the public entry point ─────────────────

@pytest.mark.asyncio
async def test_suggest_rebalance_uses_the_floored_window(session_factory):
    """Lapsed roster, driven through ``suggest_rebalance``.

    The fixture clears BOTH short-circuits — ``empty_no_history`` (Groceries
    and Dining both have settled history inside the trailing 3-month window)
    and ``empty_no_surplus`` (Dining is projected well under budget) — so the
    response actually reaches the window-dependent allocation.

    Three distinct outcomes distinguish the three implementations:

    * **floored (correct)** — Groceries actual 500 → deficit 100, fully covered
      by Dining's 180 surplus. ``uncovered_overspend == 0``.
    * **unbounded (``main``)** — Groceries actual 1500 → deficit 1100, only 180
      movable, ``uncovered_overspend == 920``.
    * **unfloored derived end** — Groceries actual 0 → no deficit at all, so no
      suggestions are emitted.
    """
    seed = await _seed_base(session_factory)
    org_id = seed["org_id"]
    await _add_period(session_factory, org_id, _d(-95), None)
    await _add_period(session_factory, org_id, _d(-65), _d(-36))
    await _add_budget(
        session_factory, org_id, seed["groceries_id"], _d(-95), amount="400.00"
    )
    await _add_budget(
        session_factory, org_id, seed["dining_id"], _d(-95), amount="200.00"
    )
    # Trailing history — strictly before the period start, so it lands in the
    # 3-month window and not in the current-period rollup.
    await _add_expense(session_factory, seed, seed["groceries_id"], "300.00", _d(-100))
    await _add_expense(session_factory, seed, seed["dining_id"], "60.00", _d(-100))
    # Current period: today counts (the floor), the future row does not.
    await _add_expense(session_factory, seed, seed["groceries_id"], "500.00", _d(0))
    await _add_expense(session_factory, seed, seed["groceries_id"], "1000.00", _d(40))

    async def fake_call(*args, **kw):
        raise NoRoutingConfigured()

    with patch(
        "app.services.budget_rebalance_service.call_llm_structured",
        side_effect=fake_call,
    ):
        async with session_factory() as db:
            out = await budget_rebalance_service.suggest_rebalance(db, org_id=org_id)

    assert out.status == "ok"
    assert out.uncovered_overspend == Decimal("0.00"), (
        "an unbounded window would drag the future-dated 1000 into the "
        "projection and leave 920 uncovered"
    )
    by_cat = {s.category_id: s for s in out.suggestions}
    assert by_cat, (
        "an unfloored derived end would hide today's 500 and emit no "
        "suggestions at all"
    )
    assert by_cat[seed["groceries_id"]].suggested_amount == Decimal("500.00")
    assert by_cat[seed["dining_id"]].suggested_amount == Decimal("100.00")
    assert out.total_suggested == out.total_budget == Decimal("600.00")


# ── Tests 13-17 — D4: the two snapshot readers ─────────────────────────────

@pytest.mark.asyncio
async def test_list_and_update_agree_on_spent_for_an_open_period(session_factory):
    """Test 13 — the two surfaces must not diverge.

    This passes against ``main`` too (there both are unbounded and therefore
    agree). What it fences is the INTERMEDIATE state a partial implementation
    would ship: site 1 fixed, D4 skipped.
    """
    seed = await _seed_base(session_factory)
    org_id = seed["org_id"]
    await _add_period(session_factory, org_id, _d(-5), None)
    await _add_period(session_factory, org_id, _d(20), _d(49))
    budget_id = await _add_budget(session_factory, org_id, seed["groceries_id"], _d(-5))
    await _add_expense(session_factory, seed, seed["groceries_id"], "100.00", _d(0))
    await _add_expense(session_factory, seed, seed["groceries_id"], "50.00", _d(25))

    async with session_factory() as db:
        listed = await budget_service.list_budgets(db, org_id, period_start=_d(-5))
        updated = await budget_service.update_budget(
            db, org_id, budget_id, BudgetUpdate(amount=None)
        )

    assert updated.spent == listed[0].spent
    assert updated.spent == Decimal("100.00")


@pytest.mark.asyncio
async def test_update_budget_bounds_a_closed_period_with_null_snapshot(
    session_factory,
):
    """Test 14 — legacy shape 1 (D9): budget created while its period was
    open, closed before TBD-241's D5 shipped, so ``period_end`` is NULL
    forever. The authoritative closed window bounds it; spend moves DOWN.
    """
    seed = await _seed_base(session_factory)
    org_id = seed["org_id"]
    await _add_period(session_factory, org_id, _d(-40), _d(-11))
    await _add_period(session_factory, org_id, _d(-10), None)
    budget_id = await _add_budget(
        session_factory, org_id, seed["groceries_id"], _d(-40), end=None
    )
    await _add_expense(session_factory, seed, seed["groceries_id"], "100.00", _d(-20))
    await _add_expense(session_factory, seed, seed["groceries_id"], "50.00", _d(0))

    async with session_factory() as db:
        out = await budget_service.update_budget(
            db, org_id, budget_id, BudgetUpdate(amount=None)
        )

    assert out.spent == Decimal("100.00")


@pytest.mark.asyncio
async def test_update_budget_reads_authoritative_end_over_stale_narrow_snapshot(
    session_factory,
):
    """Test 14b — legacy shape 2 (D9): the stored snapshot is EARLIER than the
    period row's real ``end_date``, produced by the pre-TBD-241 revive followed
    by a later close. Here spend legitimately RISES, and the new value is the
    authoritative one. Pinned so the release note stays honest.
    """
    seed = await _seed_base(session_factory)
    org_id = seed["org_id"]
    await _add_period(session_factory, org_id, _d(-40), _d(-11))
    await _add_period(session_factory, org_id, _d(-10), None)
    budget_id = await _add_budget(
        session_factory, org_id, seed["groceries_id"], _d(-40), end=_d(-25)
    )
    await _add_expense(session_factory, seed, seed["groceries_id"], "100.00", _d(-30))
    await _add_expense(session_factory, seed, seed["groceries_id"], "50.00", _d(-20))

    async with session_factory() as db:
        out = await budget_service.update_budget(
            db, org_id, budget_id, BudgetUpdate(amount=None)
        )

    assert out.spent == Decimal("150.00"), (
        "the stale-narrow snapshot (-25) must lose to the period row's "
        "authoritative end (-11)"
    )
    # D5: the emitted snapshot column is untouched — derived ends are for
    # queries only.
    assert out.period_end == _d(-25)


@pytest.mark.asyncio
async def test_transfer_budget_computes_both_rows_on_one_window(session_factory):
    """Test 15 — source and target always share ``source.period_start``, so a
    single lookup serves both and neither row is unbounded.
    """
    seed = await _seed_base(session_factory)
    org_id = seed["org_id"]
    await _add_period(session_factory, org_id, _d(-5), None)
    await _add_period(session_factory, org_id, _d(20), _d(49))
    src_id = await _add_budget(
        session_factory, org_id, seed["groceries_id"], _d(-5), amount="400.00"
    )
    await _add_budget(
        session_factory, org_id, seed["dining_id"], _d(-5), amount="200.00"
    )
    await _add_expense(session_factory, seed, seed["groceries_id"], "100.00", _d(0))
    await _add_expense(session_factory, seed, seed["groceries_id"], "999.00", _d(25))
    await _add_expense(session_factory, seed, seed["dining_id"], "50.00", _d(0))
    await _add_expense(session_factory, seed, seed["dining_id"], "999.00", _d(25))

    async with session_factory() as db:
        rows = await budget_service.transfer_budget(
            db, org_id, src_id, seed["dining_id"], Decimal("50.00")
        )

    by_cat = {r.category_id: r for r in rows}
    assert by_cat[seed["groceries_id"]].spent == Decimal("100.00")
    assert by_cat[seed["dining_id"]].spent == Decimal("50.00")


@pytest.mark.asyncio
async def test_update_budget_on_a_stranded_budget_falls_back_to_the_snapshot(
    session_factory,
):
    """Test 16 — no period row at ``budget.period_start``.

    Guards against resolving through ``resolve_period``, which raises
    ``ValidationError`` on a miss and would turn a ``PUT /budgets/{id}`` into a
    400. Unreachable in production today (``org_data_service`` deletes budgets
    before periods, in the same transaction), kept as defence in depth for
    TBD-235's boundary move.
    """
    seed = await _seed_base(session_factory)
    org_id = seed["org_id"]
    await _add_period(session_factory, org_id, _d(-10), None)
    budget_id = await _add_budget(
        session_factory, org_id, seed["groceries_id"], _d(-40), end=_d(-11)
    )
    await _add_expense(session_factory, seed, seed["groceries_id"], "100.00", _d(-20))
    await _add_expense(session_factory, seed, seed["groceries_id"], "50.00", _d(0))

    async with session_factory() as db:
        out = await budget_service.update_budget(
            db, org_id, budget_id, BudgetUpdate(amount=None)
        )

    assert out.spent == Decimal("100.00"), "must fall back to the stored snapshot"


@pytest.mark.asyncio
async def test_update_budget_does_not_create_a_billing_period(session_factory):
    """Test 17 — resolving by ``budget.period_start`` removes
    ``get_current_period``'s auto-create-and-commit side effect: today a plain
    ``PUT /budgets/{id}`` on an org with no open row silently writes a period.
    """
    seed = await _seed_base(session_factory)
    org_id = seed["org_id"]
    await _add_period(session_factory, org_id, _d(-40), _d(-11))
    budget_id = await _add_budget(
        session_factory, org_id, seed["groceries_id"], _d(-40), end=_d(-11)
    )
    await _add_expense(session_factory, seed, seed["groceries_id"], "100.00", _d(-20))

    async with session_factory() as db:
        before = await db.scalar(
            select(func.count(BillingPeriod.id)).where(BillingPeriod.org_id == org_id)
        )
        await budget_service.update_budget(
            db, org_id, budget_id, BudgetUpdate(amount=None)
        )
        after = await db.scalar(
            select(func.count(BillingPeriod.id)).where(BillingPeriod.org_id == org_id)
        )

    assert before == 1
    assert after == 1, "a read-shaped PUT must not write a BillingPeriod row"


# ── D6 threading fences ────────────────────────────────────────────────────
#
# Review found that D6's entire `today=` surface — roughly twenty call sites
# across three services — had no test at all: `today=` could be deleted from
# any of them and the suite stayed green. The three tests below close that
# blind spot at one public entry point per service. They are deliberately
# NOT parameterised over every site; the point is that the wiring is proven
# somewhere in each service, so a wholesale removal cannot pass unnoticed.


@pytest.mark.asyncio
async def test_suggest_rebalance_resolves_the_clock_once_for_both_consumers(
    session_factory, monkeypatch,
):
    """The two-clocks fence for ``suggest_rebalance``.

    ``suggest_rebalance`` feeds ``today`` to TWO consumers —
    ``period_spend_window_end`` (the spend window) and ``_gather_facts`` (the
    trailing 3-month split) — separated by a ``SELECT MIN(start_date)``
    round-trip. In production nothing threads ``today``, so if the entry point
    forwards its own ``None`` instead of resolving it, each consumer defaults
    from the wall clock **independently** and a request straddling midnight
    takes its window from one day and its history split from the next.

    The clock here is scripted to advance on every read, which is exactly that
    straddle. Two things are asserted, and both go red if the resolution is
    removed from the top of ``suggest_rebalance``:

    * each consumer receives a concrete date, and it is the **same** date;
    * the wall clock is read **exactly once** for the whole computation.
    """
    seed = await _seed_base(session_factory)
    org_id = seed["org_id"]
    await _add_period(session_factory, org_id, _d(-95), None)
    await _add_period(session_factory, org_id, _d(-65), _d(-36))
    await _add_budget(
        session_factory, org_id, seed["groceries_id"], _d(-95), amount="400.00"
    )
    await _add_budget(
        session_factory, org_id, seed["dining_id"], _d(-95), amount="200.00"
    )
    await _add_expense(session_factory, seed, seed["groceries_id"], "300.00", _d(-100))
    await _add_expense(session_factory, seed, seed["dining_id"], "60.00", _d(-100))
    await _add_expense(session_factory, seed, seed["groceries_id"], "500.00", _d(0))

    scripted, clock_calls = _scripted_clock([TODAY, _d(1)])
    fake_datetime = types.SimpleNamespace(
        date=scripted, timedelta=datetime.timedelta
    )
    # Both modules, because the two default sites live in different modules:
    # `billing_service.period_spend_window_end` and
    # `budget_rebalance_service._gather_facts`.
    monkeypatch.setattr(billing_service, "datetime", fake_datetime)
    monkeypatch.setattr(budget_rebalance_service, "datetime", fake_datetime)

    # Lists, not a dict of last-seen values: a list distinguishes "called with
    # None" from "never called at all", and both are failures worth naming.
    window_todays: list[datetime.date | None] = []
    gather_todays: list[datetime.date | None] = []
    real_window = budget_rebalance_service.period_spend_window_end
    real_gather = budget_rebalance_service._gather_facts

    async def spy_window(db, org_id_, period, *, today=None):
        window_todays.append(today)
        return await real_window(db, org_id_, period, today=today)

    async def spy_gather(db, **kwargs):
        gather_todays.append(kwargs.get("today"))
        return await real_gather(db, **kwargs)

    monkeypatch.setattr(
        budget_rebalance_service, "period_spend_window_end", spy_window
    )
    monkeypatch.setattr(budget_rebalance_service, "_gather_facts", spy_gather)

    async def fake_call(*args, **kwargs):
        raise NoRoutingConfigured()

    with patch(
        "app.services.budget_rebalance_service.call_llm_structured",
        side_effect=fake_call,
    ):
        async with session_factory() as db:
            out = await budget_rebalance_service.suggest_rebalance(db, org_id=org_id)

    assert out.status == "ok"
    assert window_todays == [TODAY], (
        "the spend window must receive the date suggest_rebalance resolved; "
        f"got {window_todays!r} (None = left to default from the wall clock)"
    )
    assert gather_todays == [TODAY], (
        "the 3-month split must receive the same resolved date; "
        f"got {gather_todays!r}"
    )
    assert window_todays == gather_todays, (
        "both consumers must see the SAME resolved date"
    )
    assert clock_calls == [TODAY], (
        "the wall clock must be read exactly once per computation; a second "
        "read is the midnight-straddle hazard D6 exists to prevent, and it "
        f"would have landed on {_d(1)}"
    )


@pytest.mark.asyncio
async def test_list_budgets_injected_today_governs_the_window(session_factory):
    """``today=`` must reach ``period_spend_window_end`` from ``list_budgets``.

    Lapsed roster, derived end at ``_d(-66)``. Pinning ``today`` to ``_d(-20)``
    floors the window there, so the ``_d(-10)`` row falls outside it. The
    wall-clock run on the same fixture is the control: it must include that
    row. If ``today=today`` were dropped from the call, the two runs would be
    identical and the first assertion goes red.
    """
    seed = await _seed_base(session_factory)
    org_id = seed["org_id"]
    await _add_period(session_factory, org_id, _d(-95), None)
    await _add_period(session_factory, org_id, _d(-65), _d(-36))
    await _add_budget(session_factory, org_id, seed["groceries_id"], _d(-95))
    await _add_expense(session_factory, seed, seed["groceries_id"], "100.00", _d(-30))
    await _add_expense(session_factory, seed, seed["groceries_id"], "50.00", _d(-10))

    async with session_factory() as db:
        pinned = await budget_service.list_budgets(
            db, org_id, period_start=_d(-95), today=_d(-20)
        )
        wall = await budget_service.list_budgets(db, org_id, period_start=_d(-95))

    assert pinned[0].spent == Decimal("100.00"), (
        "the injected today must floor the window at _d(-20), excluding the "
        "_d(-10) row"
    )
    assert wall[0].spent == Decimal("150.00"), (
        "control: with the real clock both rows are inside the window"
    )


@pytest.mark.asyncio
async def test_forecast_actuals_injected_today_governs_the_window(session_factory):
    """Same fence for ``forecast_plan_service``, through ``get_plan_for_period``.

    This is the one of eleven ``_build_response`` call sites that is proven.
    """
    seed = await _seed_base(session_factory)
    org_id = seed["org_id"]
    period_id = await _add_period(session_factory, org_id, _d(-95), None)
    await _add_period(session_factory, org_id, _d(-65), _d(-36))
    await _add_plan_with_item(session_factory, org_id, period_id, seed["groceries_id"])
    await _add_expense(session_factory, seed, seed["groceries_id"], "100.00", _d(-30))
    await _add_expense(session_factory, seed, seed["groceries_id"], "50.00", _d(-10))

    async with session_factory() as db:
        pinned = await forecast_plan_service.get_plan_for_period(
            db, org_id, period_start=_d(-95), today=_d(-20)
        )
        wall = await forecast_plan_service.get_plan_for_period(
            db, org_id, period_start=_d(-95)
        )

    assert pinned is not None and wall is not None
    assert pinned.total_actual_expense == Decimal("100.00"), (
        "the injected today must floor the actuals window at _d(-20)"
    )
    assert wall.total_actual_expense == Decimal("150.00"), (
        "control: with the real clock both rows are inside the window"
    )


# ── Test 16b — transfer_budget's own stranded fallback ─────────────────────

@pytest.mark.asyncio
async def test_transfer_budget_on_a_stranded_budget_falls_back_to_source_snapshot(
    session_factory,
):
    """The transfer sibling of test 16, which does **not** cover this branch.

    ``update_budget``'s fallback reads ``budget.period_end``;
    ``transfer_budget``'s reads ``source.period_end`` and serves BOTH rows from
    that one lookup. Different snapshot, two consumers — so it is not covered
    transitively, and it is new code on a money path.

    The target budget is given a deliberately DIFFERENT stored snapshot
    (``_d(-25)``) so the test discriminates: reading the target's own snapshot
    would drop its ``_d(-20)`` row and report 0. An unbounded fallback would
    report the ``_d(0)`` rows too.
    """
    seed = await _seed_base(session_factory)
    org_id = seed["org_id"]
    # A period row exists, but NOT at the budgets' period_start — stranded.
    await _add_period(session_factory, org_id, _d(-10), None)
    src_id = await _add_budget(
        session_factory, org_id, seed["groceries_id"], _d(-40),
        end=_d(-11), amount="400.00",
    )
    await _add_budget(
        session_factory, org_id, seed["dining_id"], _d(-40),
        end=_d(-25), amount="200.00",
    )
    await _add_expense(session_factory, seed, seed["groceries_id"], "100.00", _d(-30))
    await _add_expense(session_factory, seed, seed["groceries_id"], "50.00", _d(0))
    await _add_expense(session_factory, seed, seed["dining_id"], "70.00", _d(-20))
    await _add_expense(session_factory, seed, seed["dining_id"], "999.00", _d(0))

    async with session_factory() as db:
        rows = await budget_service.transfer_budget(
            db, org_id, src_id, seed["dining_id"], Decimal("50.00")
        )

    by_cat = {r.category_id: r for r in rows}
    # The transfer itself still happened — the fallback must not 400 or throw.
    assert by_cat[seed["groceries_id"]].amount == Decimal("350.00")
    assert by_cat[seed["dining_id"]].amount == Decimal("250.00")
    assert by_cat[seed["groceries_id"]].spent == Decimal("100.00"), (
        "bounded by source.period_end (_d(-11)); the _d(0) row is out"
    )
    assert by_cat[seed["dining_id"]].spent == Decimal("70.00"), (
        "the TARGET row is bounded by the SOURCE's snapshot too — its own "
        "_d(-25) snapshot would have excluded the _d(-20) row"
    )
