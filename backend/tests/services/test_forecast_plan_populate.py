"""Tests for forecast_plan_service.populate_from_sources and
refresh_from_sources — pins:

1. Recurring INCOME templates feed populate symmetrically with EXPENSE.
2. Historical query is SETTLED-only and excludes the current period.
3. Current-period query counts SETTLED + PENDING and acts as one extra
   month slot in the rolling average.
4. refresh_from_sources drops auto-generated items (recurring|history)
   and preserves user-edited (manual) items, then re-runs populate.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.account import Account, AccountType
from app.models.billing import BillingPeriod
from app.models.category import Category, CategoryType
from app.models.forecast_plan import (
    ForecastItemType,
    ForecastPlan,
    ForecastPlanItem,
    ItemSource,
    PlanStatus,
)
from app.models.recurring import Frequency, RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization
from app.services import forecast_plan_service


@pytest_asyncio.fixture
async def session_factory():
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
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed(factory: async_sessionmaker[AsyncSession]) -> dict:
    """Org with one open billing period (May 2026) + master Groceries +
    master Salary + an account."""
    org_id = 1
    may_start = datetime.date(2026, 5, 1)
    may_end = datetime.date(2026, 5, 31)

    async with factory() as db:
        db.add(Organization(id=org_id, name="org", billing_cycle_day=1))
        await db.commit()

        at = AccountType(org_id=org_id, name="Cash", slug="cash", is_system=True)
        db.add(at)
        await db.commit()

        acc = Account(
            org_id=org_id, account_type_id=at.id, name="Wallet",
            balance=Decimal("0"),
        )
        db.add(acc)
        await db.commit()

        groceries = Category(
            org_id=org_id, name="Groceries",
            slug="groceries", type=CategoryType.EXPENSE,
        )
        salary = Category(
            org_id=org_id, name="Salary",
            slug="salary", type=CategoryType.INCOME,
        )
        db.add_all([groceries, salary])
        await db.commit()

        period = BillingPeriod(
            org_id=org_id, start_date=may_start, end_date=may_end,
        )
        db.add(period)
        await db.commit()

        return {
            "org_id": org_id,
            "account_id": acc.id,
            "groceries_id": groceries.id,
            "salary_id": salary.id,
            "may_start": may_start,
            "may_end": may_end,
            "period_id": period.id,
        }


# ── Bug 2: recurring INCOME flows into populate ──────────────────────────────


@pytest.mark.asyncio
async def test_recurring_income_populates_plan(session_factory):
    """A monthly recurring INCOME template due in the period must show up
    as a plan item with source=recurring. Symmetric with EXPENSE."""
    seed = await _seed(session_factory)

    async with session_factory() as db:
        # Monthly salary; due May 25
        db.add(RecurringTransaction(
            org_id=seed["org_id"],
            account_id=seed["account_id"],
            category_id=seed["salary_id"],
            description="Salary",
            amount=Decimal("3000"),
            type="income",
            frequency=Frequency.MONTHLY,
            next_due_date=datetime.date(2026, 5, 25),
            auto_settle=False,
            is_active=True,
        ))
        # Monthly grocery bill; due May 5
        db.add(RecurringTransaction(
            org_id=seed["org_id"],
            account_id=seed["account_id"],
            category_id=seed["groceries_id"],
            description="Weekly grocery",
            amount=Decimal("400"),
            type="expense",
            frequency=Frequency.MONTHLY,
            next_due_date=datetime.date(2026, 5, 5),
            auto_settle=False,
            is_active=True,
        ))
        await db.commit()

    async with session_factory() as db:
        resp = await forecast_plan_service.populate_from_sources(
            db, org_id=seed["org_id"], period_start=seed["may_start"],
        )

    by_key = {(it.category_id, it.type): it for it in resp.items}
    inc = by_key.get((seed["salary_id"], "income"))
    exp = by_key.get((seed["groceries_id"], "expense"))

    assert inc is not None, "recurring income should populate a plan item"
    assert inc.planned_amount == Decimal("3000")
    assert inc.source == "recurring"

    assert exp is not None, "recurring expense should populate a plan item"
    assert exp.planned_amount == Decimal("400")
    assert exp.source == "recurring"


# ── Bug 3: current-period pending counts; history pending does not ──────────


def _tx(*, org_id, account_id, category_id, amount, date, settled_date,
        status, type_, linked_transaction_id=None) -> Transaction:
    return Transaction(
        org_id=org_id, account_id=account_id, category_id=category_id,
        description="t", amount=Decimal(str(amount)), type=type_,
        status=status, date=date, settled_date=settled_date,
        linked_transaction_id=linked_transaction_id,
    )


@pytest.mark.asyncio
async def test_current_period_pending_counts_history_pending_excluded(session_factory):
    """Pending in the current period adds a slot to the average; pending
    in history months is ignored."""
    seed = await _seed(session_factory)

    async with session_factory() as db:
        # History settled — Feb 2026 expense $100
        db.add(_tx(
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=seed["groceries_id"], amount=100,
            date=datetime.date(2026, 2, 10),
            settled_date=datetime.date(2026, 2, 10),
            status=TransactionStatus.SETTLED, type_=TransactionType.EXPENSE,
        ))
        # History settled — Mar 2026 expense $200
        db.add(_tx(
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=seed["groceries_id"], amount=200,
            date=datetime.date(2026, 3, 10),
            settled_date=datetime.date(2026, 3, 10),
            status=TransactionStatus.SETTLED, type_=TransactionType.EXPENSE,
        ))
        # History PENDING — Apr 2026 expense $9999 — must be ignored
        db.add(_tx(
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=seed["groceries_id"], amount=9999,
            date=datetime.date(2026, 4, 10),
            settled_date=None,
            status=TransactionStatus.PENDING, type_=TransactionType.EXPENSE,
        ))
        # Current period PENDING — May 2026 expense $300 — counts
        db.add(_tx(
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=seed["groceries_id"], amount=300,
            date=datetime.date(2026, 5, 5),
            settled_date=None,
            status=TransactionStatus.PENDING, type_=TransactionType.EXPENSE,
        ))
        await db.commit()

    async with session_factory() as db:
        resp = await forecast_plan_service.populate_from_sources(
            db, org_id=seed["org_id"], period_start=seed["may_start"],
        )

    # Two history months ($100, $200) + current period ($300, pending) = 3 slots
    # Average = (100 + 200 + 300) / 3 = 200.00
    items = [
        i for i in resp.items
        if i.category_id == seed["groceries_id"] and i.type == "expense"
    ]
    assert len(items) == 1
    assert items[0].source == "history"
    assert items[0].planned_amount == Decimal("200.00")


@pytest.mark.asyncio
async def test_history_settled_only(session_factory):
    """Two history months, no current-period activity. History pending
    must not count even when current-period scope is empty."""
    seed = await _seed(session_factory)

    async with session_factory() as db:
        # Feb 2026 settled $200, Mar 2026 settled $400
        db.add(_tx(
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=seed["groceries_id"], amount=200,
            date=datetime.date(2026, 2, 10),
            settled_date=datetime.date(2026, 2, 10),
            status=TransactionStatus.SETTLED, type_=TransactionType.EXPENSE,
        ))
        db.add(_tx(
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=seed["groceries_id"], amount=400,
            date=datetime.date(2026, 3, 10),
            settled_date=datetime.date(2026, 3, 10),
            status=TransactionStatus.SETTLED, type_=TransactionType.EXPENSE,
        ))
        # Apr pending — must NOT factor in
        db.add(_tx(
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=seed["groceries_id"], amount=9999,
            date=datetime.date(2026, 4, 15),
            settled_date=None,
            status=TransactionStatus.PENDING, type_=TransactionType.EXPENSE,
        ))
        await db.commit()

    async with session_factory() as db:
        resp = await forecast_plan_service.populate_from_sources(
            db, org_id=seed["org_id"], period_start=seed["may_start"],
        )

    items = [
        i for i in resp.items
        if i.category_id == seed["groceries_id"] and i.type == "expense"
    ]
    assert len(items) == 1
    # Average of only the two settled months = (200 + 400) / 2 = 300
    assert items[0].planned_amount == Decimal("300.00")


@pytest.mark.asyncio
async def test_current_period_excludes_transfer_legs(session_factory):
    """A pending transfer leg (linked_transaction_id != NULL) in the
    current period must not feed the suggestion — same rule as actuals."""
    seed = await _seed(session_factory)

    async with session_factory() as db:
        # One settled history month so the threshold of 2 slots is met
        db.add(_tx(
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=seed["groceries_id"], amount=100,
            date=datetime.date(2026, 3, 10),
            settled_date=datetime.date(2026, 3, 10),
            status=TransactionStatus.SETTLED, type_=TransactionType.EXPENSE,
        ))
        # Transfer pair in May — both halves linked, must be excluded
        a = _tx(
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=seed["groceries_id"], amount=500,
            date=datetime.date(2026, 5, 10),
            settled_date=None,
            status=TransactionStatus.PENDING, type_=TransactionType.EXPENSE,
        )
        db.add(a)
        await db.commit()
        b = _tx(
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=seed["groceries_id"], amount=500,
            date=datetime.date(2026, 5, 10),
            settled_date=None,
            status=TransactionStatus.PENDING, type_=TransactionType.INCOME,
            linked_transaction_id=a.id,
        )
        db.add(b)
        await db.commit()
        a.linked_transaction_id = b.id
        await db.commit()

    async with session_factory() as db:
        resp = await forecast_plan_service.populate_from_sources(
            db, org_id=seed["org_id"], period_start=seed["may_start"],
        )

    # Only one history month + zero current-period reportable activity =
    # only one slot, below the len < 2 threshold; nothing should populate
    # for groceries.
    items = [
        i for i in resp.items
        if i.category_id == seed["groceries_id"] and i.type == "expense"
    ]
    assert items == []


# ── Bug 5: refresh_from_sources preserves manual, replaces auto ─────────────


@pytest.mark.asyncio
async def test_refresh_replaces_auto_generated_preserves_manual(session_factory):
    """A plan with one MANUAL item, one RECURRING item, one HISTORY item.
    After refresh: MANUAL stays, RECURRING + HISTORY are replaced by
    fresh populate output."""
    seed = await _seed(session_factory)

    async with session_factory() as db:
        plan = ForecastPlan(
            org_id=seed["org_id"], billing_period_id=seed["period_id"],
            status=PlanStatus.DRAFT,
        )
        db.add(plan)
        await db.commit()

        # Manual user-entered item — must survive
        db.add(ForecastPlanItem(
            plan_id=plan.id, org_id=seed["org_id"],
            category_id=seed["groceries_id"],
            type=ForecastItemType.EXPENSE,
            planned_amount=Decimal("777"),
            source=ItemSource.MANUAL,
        ))
        # Auto-generated stale items — must be dropped
        db.add(ForecastPlanItem(
            plan_id=plan.id, org_id=seed["org_id"],
            category_id=seed["salary_id"],
            type=ForecastItemType.INCOME,
            planned_amount=Decimal("1"),
            source=ItemSource.RECURRING,
        ))
        await db.commit()

        # Add a fresh recurring template that should appear after refresh
        # (different category to avoid colliding with the manual groceries item)
        utilities = Category(
            org_id=seed["org_id"], name="Utilities",
            slug="utilities", type=CategoryType.EXPENSE,
        )
        db.add(utilities)
        await db.commit()
        utilities_id = utilities.id

        db.add(RecurringTransaction(
            org_id=seed["org_id"],
            account_id=seed["account_id"],
            category_id=utilities_id,
            description="Power",
            amount=Decimal("80"),
            type="expense",
            frequency=Frequency.MONTHLY,
            next_due_date=datetime.date(2026, 5, 15),
            auto_settle=False,
            is_active=True,
        ))
        # Fresh recurring income with new amount — replaces the stale $1 item
        db.add(RecurringTransaction(
            org_id=seed["org_id"],
            account_id=seed["account_id"],
            category_id=seed["salary_id"],
            description="Salary",
            amount=Decimal("3500"),
            type="income",
            frequency=Frequency.MONTHLY,
            next_due_date=datetime.date(2026, 5, 25),
            auto_settle=False,
            is_active=True,
        ))
        await db.commit()

    async with session_factory() as db:
        resp = await forecast_plan_service.refresh_from_sources(
            db, org_id=seed["org_id"], period_start=seed["may_start"],
        )

    by_key = {(it.category_id, it.type): it for it in resp.items}

    manual_item = by_key.get((seed["groceries_id"], "expense"))
    assert manual_item is not None, "manual item must survive refresh"
    assert manual_item.planned_amount == Decimal("777")
    assert manual_item.source == "manual"

    refreshed_inc = by_key.get((seed["salary_id"], "income"))
    assert refreshed_inc is not None
    assert refreshed_inc.planned_amount == Decimal("3500")
    assert refreshed_inc.source == "recurring"

    new_utilities = by_key.get((utilities_id, "expense"))
    assert new_utilities is not None
    assert new_utilities.planned_amount == Decimal("80")
    assert new_utilities.source == "recurring"

    # No leftover stale RECURRING/HISTORY items beyond what populate now
    # generates — confirm no item with source=recurring|history that
    # doesn't match the fresh templates
    stale_residue = [
        i for i in resp.items
        if i.source in ("recurring", "history")
        and i.category_id not in {seed["salary_id"], utilities_id}
    ]
    assert stale_residue == []


@pytest.mark.asyncio
async def test_refresh_when_no_plan_items_yet(session_factory):
    """Refresh on an empty plan is equivalent to populate — no items to
    delete, populate runs and inserts whatever sources produce."""
    seed = await _seed(session_factory)

    async with session_factory() as db:
        db.add(RecurringTransaction(
            org_id=seed["org_id"],
            account_id=seed["account_id"],
            category_id=seed["salary_id"],
            description="Salary",
            amount=Decimal("3000"),
            type="income",
            frequency=Frequency.MONTHLY,
            next_due_date=datetime.date(2026, 5, 25),
            auto_settle=False,
            is_active=True,
        ))
        await db.commit()

    async with session_factory() as db:
        resp = await forecast_plan_service.refresh_from_sources(
            db, org_id=seed["org_id"], period_start=seed["may_start"],
        )

    items = [
        i for i in resp.items
        if i.category_id == seed["salary_id"] and i.type == "income"
    ]
    assert len(items) == 1
    assert items[0].planned_amount == Decimal("3000")


@pytest.mark.asyncio
async def test_refresh_does_not_duplicate_manual_items(session_factory):
    """If a manual item already exists for a category, populate's
    additive logic must skip the (cat, type) key — the manual item
    stays, no duplicate row."""
    seed = await _seed(session_factory)

    async with session_factory() as db:
        plan = ForecastPlan(
            org_id=seed["org_id"], billing_period_id=seed["period_id"],
            status=PlanStatus.DRAFT,
        )
        db.add(plan)
        await db.commit()
        # Manual income item; user set $5000 by hand
        db.add(ForecastPlanItem(
            plan_id=plan.id, org_id=seed["org_id"],
            category_id=seed["salary_id"],
            type=ForecastItemType.INCOME,
            planned_amount=Decimal("5000"),
            source=ItemSource.MANUAL,
        ))
        await db.commit()

        # Active recurring of $3000 for the same (category, type)
        db.add(RecurringTransaction(
            org_id=seed["org_id"],
            account_id=seed["account_id"],
            category_id=seed["salary_id"],
            description="Salary",
            amount=Decimal("3000"),
            type="income",
            frequency=Frequency.MONTHLY,
            next_due_date=datetime.date(2026, 5, 25),
            auto_settle=False,
            is_active=True,
        ))
        await db.commit()

    async with session_factory() as db:
        resp = await forecast_plan_service.refresh_from_sources(
            db, org_id=seed["org_id"], period_start=seed["may_start"],
        )

    items = [
        i for i in resp.items
        if i.category_id == seed["salary_id"] and i.type == "income"
    ]
    assert len(items) == 1, "manual item must not be duplicated by recurring"
    assert items[0].planned_amount == Decimal("5000")
    assert items[0].source == "manual"
