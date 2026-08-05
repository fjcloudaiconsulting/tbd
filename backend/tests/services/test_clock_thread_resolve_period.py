"""TBD-299 — the two services that resolve a clock and then drop it at
``resolve_period``.

TBD-297 gave ``billing_service.resolve_period`` a ``today=`` pass-through
because it is the *intermediary* that hides ``get_current_period``'s
auto-create from its callers: a caller holding a resolved clock calls
``resolve_period``, looks clock-safe, and still anchors a brand-new
``BillingPeriod`` to a second, independently-read clock. That anchor is a
money-row anchor — every budget, plan and report for the org's first period
hangs off it.

``budget_service`` and ``forecast_plan_service`` each take an injectable
``today`` and each dropped it at every ``resolve_period`` call. These fences
pin the thread at the eight call sites.

Every *fence* date below is 2099, which the wall clock cannot equal — a fence
for "uses the injected clock, not ``date.today()``" is vacuous the moment the
two can coincide (``reference_wall_clock_date_bomb_tests``, class 2).

⚠ There are SEVEN fences and ONE guard, and the distinction matters when
reading a green run. The ``today=None`` guard at the bottom re-reads
``date.today()`` on purpose: what it pins is "omitting the argument still
works", not "the injected clock won". It is green against unmodified ``main``
by design, so it is not evidence the thread is present. Only the seven 2099
assertions are.

``cycle_day`` is 15 everywhere, deliberately NOT 1: ``get_current_period``'s
no-org fallback is also 1, so a fence anchored at ``cycle_day=1`` passes
against an implementation that ignores ``org.billing_cycle_day`` entirely.
Asserting ``2099-03-15`` kills both wrong implementations at once — the one
that reads the wall clock, and the one that ignores the org's cycle day.

Every assertion reads the auto-created row's ``start_date`` from a FRESH
session: the anchor itself, committed, not a quantity derived from it.

Not fenced, and deliberately so: ``forecast_plan_service.copy_from_period``'s
SECOND ``resolve_period`` (the ``source_period_start`` one) takes a required,
non-optional date, so it can never reach the fallback arm and can never
auto-create. Threading ``today=`` there is defensive consistency, not
observable behaviour; a "fence" for it would be green against both
implementations. See the ticket report rather than a manufactured test.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.billing import BillingPeriod
from app.models.category import Category, CategoryType
from app.models.user import Organization
from app.schemas.budget import BudgetCreate
from app.services import billing_service, budget_service, forecast_plan_service
from app.services.exceptions import ValidationError

INJECTED = datetime.date(2099, 3, 17)
EXPECTED_ANCHOR = datetime.date(2099, 3, 15)
CYCLE_DAY = 15
ORG_ID = 1


@pytest_asyncio.fixture
async def session_factory():
    """In-memory SQLite shared across sessions via StaticPool."""
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


async def _seed_org_without_any_period(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An org with NO ``BillingPeriod`` at all — the auto-create precondition."""
    async with factory() as db:
        db.add(
            Organization(id=ORG_ID, name="test-org", billing_cycle_day=CYCLE_DAY)
        )
        await db.commit()


async def _seed_master_category(
    factory: async_sessionmaker[AsyncSession],
) -> int:
    async with factory() as db:
        cat = Category(
            org_id=ORG_ID, name="Groceries", type=CategoryType.EXPENSE, parent_id=None
        )
        db.add(cat)
        await db.commit()
        return cat.id


async def _anchors(factory: async_sessionmaker[AsyncSession]) -> list[datetime.date]:
    """Every committed ``BillingPeriod`` anchor for the org, read fresh."""
    async with factory() as db:
        rows = (
            await db.execute(
                select(BillingPeriod).where(BillingPeriod.org_id == ORG_ID)
            )
        ).scalars().all()
    return [r.start_date for r in rows]


def _msg(where: str) -> str:
    return (
        f"{where} auto-created a BillingPeriod anchored to the WALL CLOCK, not "
        f"to the caller's injected clock. The service resolved a `today` and "
        f"then dropped it at `resolve_period`, so a money-row anchor was set by "
        f"a second, independent clock read (TBD-299)."
    )


# ── budget_service ───────────────────────────────────────────────────────────


async def test_list_budgets_threads_its_clock_into_the_auto_create(session_factory):
    """FENCE — budget_service.list_budgets, `resolve_period` call site 1 of 2.

    Wrong implementation killed: `resolve_period(db, org_id, period_start)`
    without `today=`.
    """
    await _seed_org_without_any_period(session_factory)

    async with session_factory() as db:
        await budget_service.list_budgets(db, ORG_ID, None, today=INJECTED)

    assert await _anchors(session_factory) == [EXPECTED_ANCHOR], _msg("list_budgets")


async def test_create_budget_threads_its_clock_into_the_auto_create(session_factory):
    """FENCE — budget_service.create_budget, `resolve_period` call site 2 of 2."""
    await _seed_org_without_any_period(session_factory)
    cat_id = await _seed_master_category(session_factory)

    async with session_factory() as db:
        await budget_service.create_budget(
            db,
            ORG_ID,
            BudgetCreate(category_id=cat_id, amount=Decimal("100.00")),
            None,
            today=INJECTED,
        )

    assert await _anchors(session_factory) == [EXPECTED_ANCHOR], _msg("create_budget")


async def test_list_budgets_still_uses_the_wall_clock_when_none_given(session_factory):
    """GUARD — omitting `today` is what every production caller does today
    (`routers/budgets.py`), and must keep the previous behaviour rather than
    crash or anchor to something arbitrary.
    """
    await _seed_org_without_any_period(session_factory)
    expected, _ = billing_service.current_cycle_window(
        CYCLE_DAY, datetime.date.today()
    )

    async with session_factory() as db:
        await budget_service.list_budgets(db, ORG_ID, None)

    assert await _anchors(session_factory) == [expected]


# ── forecast_plan_service ────────────────────────────────────────────────────


async def test_get_or_create_plan_threads_its_clock_into_the_auto_create(
    session_factory,
):
    """FENCE — forecast_plan_service.get_or_create_plan."""
    await _seed_org_without_any_period(session_factory)

    async with session_factory() as db:
        await forecast_plan_service.get_or_create_plan(
            db, ORG_ID, None, today=INJECTED
        )

    assert await _anchors(session_factory) == [EXPECTED_ANCHOR], _msg(
        "get_or_create_plan"
    )


async def test_get_plan_for_period_threads_its_clock_into_the_auto_create(
    session_factory,
):
    """FENCE — forecast_plan_service.get_plan_for_period.

    ⚠ This function is documented as side-effect-free ("visiting the Dashboard
    doesn't pollute the database with empty drafts") — that promise covers the
    *plan* row only. `resolve_period`'s fallback still auto-creates the
    *period*, which is precisely why the clock has to reach it.
    """
    await _seed_org_without_any_period(session_factory)

    async with session_factory() as db:
        assert (
            await forecast_plan_service.get_plan_for_period(
                db, ORG_ID, None, today=INJECTED
            )
            is None
        )

    assert await _anchors(session_factory) == [EXPECTED_ANCHOR], _msg(
        "get_plan_for_period"
    )


async def test_populate_from_sources_threads_its_clock_into_the_auto_create(
    session_factory,
):
    """FENCE — forecast_plan_service.populate_from_sources."""
    await _seed_org_without_any_period(session_factory)

    async with session_factory() as db:
        await forecast_plan_service.populate_from_sources(
            db, ORG_ID, None, today=INJECTED
        )

    assert await _anchors(session_factory) == [EXPECTED_ANCHOR], _msg(
        "populate_from_sources"
    )


async def test_refresh_from_sources_threads_its_clock_into_the_auto_create(
    session_factory,
):
    """FENCE — forecast_plan_service.refresh_from_sources.

    Its inner `populate_from_sources` call already threaded `today`; the
    `resolve_period` immediately above it did not. Fenced independently
    because the inner thread makes the outer drop invisible to any test that
    only exercises the populate path.
    """
    await _seed_org_without_any_period(session_factory)

    async with session_factory() as db:
        await forecast_plan_service.refresh_from_sources(
            db, ORG_ID, None, today=INJECTED
        )

    assert await _anchors(session_factory) == [EXPECTED_ANCHOR], _msg(
        "refresh_from_sources"
    )


async def test_copy_from_period_threads_its_clock_into_the_target_auto_create(
    session_factory,
):
    """FENCE — forecast_plan_service.copy_from_period, the TARGET resolve.

    `target_period_start=None` is the only arm of this function that can reach
    the auto-create. The source resolve that follows raises `ValidationError`
    (no such period, and no plan to copy either way) — irrelevant to the fence,
    because the target's period row is already committed by then. Reading the
    committed anchor rather than the return value is what lets this test exist
    at all.
    """
    await _seed_org_without_any_period(session_factory)

    async with session_factory() as db:
        with pytest.raises(ValidationError):
            await forecast_plan_service.copy_from_period(
                db,
                ORG_ID,
                None,
                datetime.date(2099, 1, 15),
                today=INJECTED,
            )

    assert await _anchors(session_factory) == [EXPECTED_ANCHOR], _msg(
        "copy_from_period"
    )
