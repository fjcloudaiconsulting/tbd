"""Tests for forecast_plan_service.populate_from_sources and
refresh_from_sources — pins:

1. Recurring INCOME templates feed populate symmetrically with EXPENSE.
2. Historical query is SETTLED-only and excludes the current period.
3. Current-period query counts SETTLED + PENDING. Since TBD-764 it is B'
   in ``R + max(H', B')``, no longer one extra slot of the average.
4. refresh_from_sources drops auto-generated items (recurring|history)
   and preserves user-edited (manual) items, then re-runs populate.
5. TBD-764: every group runs one computation, R + max(H', B'), so mixed
   template + variable groups and manual pending rows are planned.
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
    """Pending in the current period counts (as B'); pending in history
    months is ignored."""
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

    # H' = two settled history months ($100, $200) -> 150.00; the Apr pending
    # $9999 stays out. B' = current period ($300, pending).
    # TBD-764: planned = max(H', B') = 300.00. It was (100 + 200 + 300) / 3 =
    # 200.00 while the current period was one slot of the average.
    items = [
        i for i in resp.items
        if i.category_id == seed["groceries_id"] and i.type == "expense"
    ]
    assert len(items) == 1
    assert items[0].source == "history"
    assert items[0].planned_amount == Decimal("300.00")


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
async def test_current_period_only_creates_plan_line_without_history(session_factory):
    """A category with NO historical settled transactions but at least one
    current-period transaction (settled or pending) must still produce a
    plan line. Current-period activity is authoritative; the 2-month
    minimum gate only applies when relying purely on history.

    Regression: before this fix, populate skipped any category whose only
    activity was in the current period, so a non-recurring one-shot like
    a furniture purchase imported in May would not surface in auto-populate
    even though pending was being counted into master_monthly.
    """
    seed = await _seed(session_factory)

    async with session_factory() as db:
        # No historical transactions at all. One pending in current period.
        db.add(_tx(
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=seed["groceries_id"], amount=500,
            date=datetime.date(2026, 5, 7),
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
    assert len(items) == 1, (
        "current-period-only activity should create a plan line; "
        "historical 2-month gate must not apply when current-period exists"
    )
    # Average over the single current-period slot equals the tx amount.
    assert items[0].planned_amount == Decimal("500.00")
    assert items[0].source == "history"


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


# ── Cash-basis: history window + bucketing use the effective settled date ──


@pytest.mark.asyncio
async def test_history_buckets_by_effective_settled_date(session_factory):
    """A GBLT (dated 2026-01-31, settled 2026-02-15) falls in the Feb
    history month by its SETTLED date — it's both inside the [Feb 1, May 1)
    history window and bucketed into Feb, not Jan. Paired with a Mar settled
    row that gives the 2nd month the 2-month minimum needs, so the plan line
    only surfaces when the GBLT is counted by its effective date.
    """
    seed = await _seed(session_factory)

    async with session_factory() as db:
        # GBLT: dated Jan 31 (before the Feb-1 history floor on raw date),
        # settled Feb 15 (inside the window, buckets to Feb).
        db.add(_tx(
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=seed["groceries_id"], amount=200,
            date=datetime.date(2026, 1, 31),
            settled_date=datetime.date(2026, 2, 15),
            status=TransactionStatus.SETTLED, type_=TransactionType.EXPENSE,
        ))
        # Mar 2026 settled $400 — the second history month.
        db.add(_tx(
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=seed["groceries_id"], amount=400,
            date=datetime.date(2026, 3, 10),
            settled_date=datetime.date(2026, 3, 10),
            status=TransactionStatus.SETTLED, type_=TransactionType.EXPENSE,
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
    # GBLT in Feb ($200) + Mar ($400) → 2 history months → avg = 300.00.
    assert len(items) == 1
    assert items[0].source == "history"
    assert items[0].planned_amount == Decimal("300.00")


# ── TBD-764: one per-group computation, planned = R + max(H', B') ───────────
#
# R  = materialised occurrences (recurring_id IS NOT NULL) by effective date,
#      plus the projection of each active template's occurrences that have no
#      row yet.
# B' = settled + pending rows with recurring_id IS NULL in the period.
# H' = monthly average of settled history rows with recurring_id IS NULL,
#      0 unless at least two history months carry some.


async def _category(factory, seed, name: str, *, parent_id=None,
                    type_=CategoryType.EXPENSE) -> int:
    async with factory() as db:
        cat = Category(
            org_id=seed["org_id"], name=name, slug=name.lower(),
            type=type_, parent_id=parent_id,
        )
        db.add(cat)
        await db.commit()
        return cat.id


async def _template(factory, seed, category_id: int, amount, next_due_date,
                    description: str = "bill", *, type_: str = "expense",
                    is_active: bool = True) -> int:
    async with factory() as db:
        r = RecurringTransaction(
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=category_id, description=description,
            amount=Decimal(str(amount)), type=type_,
            frequency=Frequency.MONTHLY, next_due_date=next_due_date,
            auto_settle=False, is_active=is_active,
        )
        db.add(r)
        await db.commit()
        return r.id


def _row(seed, category_id: int, amount, date, *, settled_date=None,
         status=TransactionStatus.PENDING, recurring_id=None,
         reconciliation_state=None,
         type_=TransactionType.EXPENSE) -> Transaction:
    tx = _tx(
        org_id=seed["org_id"], account_id=seed["account_id"],
        category_id=category_id, amount=amount, date=date,
        settled_date=settled_date, status=status, type_=type_,
    )
    tx.recurring_id = recurring_id
    if reconciliation_state is not None:
        tx.reconciliation_state = reconciliation_state
    return tx


def _expense_items(resp, category_id: int):
    return [
        i for i in resp.items
        if i.category_id == category_id and i.type == "expense"
    ]


@pytest.mark.asyncio
async def test_mixed_group_plans_rent_plus_variable_before_and_after_generation(
    session_factory,
):
    """A Housing group with a 1500 rent template AND ~200/month of variable
    settled spend plans ~1700 both before and after the May rent occurrence
    is generated.

    Before this fix the template branch claimed the group and the history
    pass skipped it (1500, the variable spend lost); after generation the
    template's frontier left the window, the history pass took over and
    averaged the rent rows with the pending occurrence as one slot (1650).
    """
    seed = await _seed(session_factory)
    housing = await _category(session_factory, seed, "Housing")
    rent_id = await _template(
        session_factory, seed, housing, 1500, datetime.date(2026, 5, 1), "Rent",
    )

    async with session_factory() as db:
        for month, variable in ((2, 180), (3, 200), (4, 220)):
            day = datetime.date(2026, month, 1)
            # The generated rent rows of past months: R covers rent now, so
            # these must stay out of H'.
            db.add(_row(seed, housing, 1500, day, settled_date=day,
                        status=TransactionStatus.SETTLED, recurring_id=rent_id))
            mid = datetime.date(2026, month, 15)
            db.add(_row(seed, housing, variable, mid, settled_date=mid,
                        status=TransactionStatus.SETTLED))
        await db.commit()

    async with session_factory() as db:
        before = await forecast_plan_service.populate_from_sources(
            db, org_id=seed["org_id"], period_start=seed["may_start"],
        )
    [item] = _expense_items(before, housing)
    assert item.planned_amount == Decimal("1700.00")
    assert item.source == "recurring"

    from app.services import recurring_service
    async with session_factory() as db:
        result = await recurring_service.generate_due_transactions(
            db, seed["org_id"], today=datetime.date(2026, 5, 2),
        )
    assert result["generated"] == 1
    async with session_factory() as db:
        tpl = await db.get(RecurringTransaction, rent_id)
        # The template drops out of the period's template select now.
        assert tpl.next_due_date > seed["may_end"]

    async with session_factory() as db:
        after = await forecast_plan_service.refresh_from_sources(
            db, org_id=seed["org_id"], period_start=seed["may_start"],
        )
    [item] = _expense_items(after, housing)
    assert item.planned_amount == Decimal("1700.00")
    assert item.source == "recurring"


@pytest.mark.asyncio
async def test_one_off_manual_pending_bill_is_planned_in_full(session_factory):
    """A hand-keyed 1200 pending bill in a category whose history runs
    100/month plans 1200, not the 375 it got as one slot of a four-slot
    average."""
    seed = await _seed(session_factory)
    gid = seed["groceries_id"]
    async with session_factory() as db:
        for month in (2, 3, 4):
            d = datetime.date(2026, month, 10)
            db.add(_row(seed, gid, 100, d, settled_date=d,
                        status=TransactionStatus.SETTLED))
        db.add(_row(seed, gid, 1200, datetime.date(2026, 5, 20)))
        await db.commit()

    async with session_factory() as db:
        resp = await forecast_plan_service.populate_from_sources(
            db, org_id=seed["org_id"], period_start=seed["may_start"],
        )
    [item] = _expense_items(resp, gid)
    assert item.planned_amount == Decimal("1200.00")
    assert item.source == "history"


@pytest.mark.asyncio
async def test_manual_pending_in_a_template_category_adds_to_the_template(
    session_factory,
):
    """A manual pending row next to a template is added on top of it
    (R + B'), not dropped because the template claimed the group."""
    seed = await _seed(session_factory)
    utilities = await _category(session_factory, seed, "Utilities")
    await _template(
        session_factory, seed, utilities, 80, datetime.date(2026, 5, 15), "Power",
    )
    async with session_factory() as db:
        db.add(_row(seed, utilities, 50, datetime.date(2026, 5, 9)))
        await db.commit()

    async with session_factory() as db:
        resp = await forecast_plan_service.populate_from_sources(
            db, org_id=seed["org_id"], period_start=seed["may_start"],
        )
    [item] = _expense_items(resp, utilities)
    assert item.planned_amount == Decimal("130.00")
    assert item.source == "recurring"


@pytest.mark.asyncio
async def test_skipped_occurrence_counts_zero_and_is_not_reprojected(
    session_factory,
):
    """A skipped occurrence is a row (generation's existence probe finds it)
    that is out of every reportable aggregate: it adds nothing to R as a
    row, and its date must not be projected again either.

    The template's frontier is left ON the skipped date so the projection
    would reach it without the probe. The 30 manual pending row keeps the
    group alive and pins the source: no R, so HISTORY.
    """
    seed = await _seed(session_factory)
    gym = await _category(session_factory, seed, "Gym")
    rid = await _template(
        session_factory, seed, gym, 100, datetime.date(2026, 5, 10), "Gym",
    )
    async with session_factory() as db:
        db.add(_row(seed, gym, 100, datetime.date(2026, 5, 10),
                    recurring_id=rid, reconciliation_state="skipped"))
        db.add(_row(seed, gym, 30, datetime.date(2026, 5, 12)))
        await db.commit()

    async with session_factory() as db:
        resp = await forecast_plan_service.populate_from_sources(
            db, org_id=seed["org_id"], period_start=seed["may_start"],
        )
    [item] = _expense_items(resp, gym)
    assert item.planned_amount == Decimal("30.00")
    assert item.source == "history"


@pytest.mark.asyncio
async def test_materialised_occurrence_is_not_counted_twice_with_its_projection(
    session_factory,
):
    """A row for (recurring_id, date) and the template's projection of the
    same occurrence are one obligation: R counts it once."""
    seed = await _seed(session_factory)
    phone = await _category(session_factory, seed, "Phone")
    rid = await _template(
        session_factory, seed, phone, 100, datetime.date(2026, 5, 5), "Phone",
    )
    async with session_factory() as db:
        d = datetime.date(2026, 5, 5)
        db.add(_row(seed, phone, 100, d, settled_date=d,
                    status=TransactionStatus.SETTLED, recurring_id=rid))
        await db.commit()

    async with session_factory() as db:
        resp = await forecast_plan_service.populate_from_sources(
            db, org_id=seed["org_id"], period_start=seed["may_start"],
        )
    [item] = _expense_items(resp, phone)
    assert item.planned_amount == Decimal("100.00")
    assert item.source == "recurring"


@pytest.mark.asyncio
async def test_pending_occurrence_settling_next_period_counts_next_period_only(
    session_factory,
):
    """An occurrence dated May 28 whose expected settlement is June 2 counts
    in June (cash basis, by effective date) and not in May. In May the
    existence probe also stops its date being projected again.

    The frontier is left ON the row's date (May 28), so in May the probe is
    what decides: a probe windowed on the effective date would miss the row
    (it settles June 2) and project May 28 again.

    June then holds two obligations: that row, and June's own occurrence
    (June 28) projected from the same frontier.
    """
    seed = await _seed(session_factory)
    async with session_factory() as db:
        db.add(BillingPeriod(
            org_id=seed["org_id"], start_date=datetime.date(2026, 6, 1),
            end_date=datetime.date(2026, 6, 30),
        ))
        await db.commit()
    card = await _category(session_factory, seed, "Card")
    rid = await _template(
        session_factory, seed, card, 100, datetime.date(2026, 5, 28), "Card",
    )
    async with session_factory() as db:
        db.add(_row(seed, card, 100, datetime.date(2026, 5, 28),
                    settled_date=datetime.date(2026, 6, 2), recurring_id=rid))
        await db.commit()

    async with session_factory() as db:
        may = await forecast_plan_service.populate_from_sources(
            db, org_id=seed["org_id"], period_start=seed["may_start"],
        )
    assert _expense_items(may, card) == []

    async with session_factory() as db:
        june = await forecast_plan_service.populate_from_sources(
            db, org_id=seed["org_id"], period_start=datetime.date(2026, 6, 1),
        )
    [item] = _expense_items(june, card)
    assert item.planned_amount == Decimal("200.00")
    assert item.source == "recurring"


@pytest.mark.asyncio
async def test_subcategory_mode_runs_the_same_computation_per_sub(session_factory):
    """Subcategory mode groups by the row's own sub: the Rent sub gets its
    template plus its manual pending row, the Utilities sub its history
    average, and the Housing master gets no item of its own."""
    from app.models.settings import OrgSetting
    from app.services.settings_service import FORECAST_INPUT_GRANULARITY_KEY

    seed = await _seed(session_factory)
    async with session_factory() as db:
        db.add(OrgSetting(
            org_id=seed["org_id"], key=FORECAST_INPUT_GRANULARITY_KEY,
            value="subcategory",
        ))
        await db.commit()
    housing = await _category(session_factory, seed, "Housing")
    rent = await _category(session_factory, seed, "Rent", parent_id=housing)
    utilities = await _category(session_factory, seed, "Utilities", parent_id=housing)
    await _template(
        session_factory, seed, rent, 1500, datetime.date(2026, 5, 1), "Rent",
    )
    async with session_factory() as db:
        db.add(_row(seed, rent, 60, datetime.date(2026, 5, 3)))
        for month, amount in ((3, 90), (4, 110)):
            d = datetime.date(2026, month, 20)
            db.add(_row(seed, utilities, amount, d, settled_date=d,
                        status=TransactionStatus.SETTLED))
        await db.commit()

    async with session_factory() as db:
        resp = await forecast_plan_service.populate_from_sources(
            db, org_id=seed["org_id"], period_start=seed["may_start"],
        )
    [rent_item] = _expense_items(resp, rent)
    assert rent_item.planned_amount == Decimal("1560.00")
    assert rent_item.source == "recurring"
    [util_item] = _expense_items(resp, utilities)
    assert util_item.planned_amount == Decimal("100.00")
    assert util_item.source == "history"
    assert _expense_items(resp, housing) == []


# ── TBD-764 review: history paid BEFORE the template existed ────────────────
#
# Onboarding order is import first, set up templates second, so the history
# of a bill is usually plain rows (recurring_id NULL), and promoting one row
# leaves the older ones NULL too. H' must not count those again next to R.


def _settled(seed, category_id: int, amount, date, **kw) -> Transaction:
    return _row(seed, category_id, amount, date, settled_date=date,
                status=TransactionStatus.SETTLED, **kw)


async def _populate(factory, seed, period_start=None):
    async with factory() as db:
        return await forecast_plan_service.populate_from_sources(
            db, org_id=seed["org_id"],
            period_start=period_start or seed["may_start"],
        )


@pytest.mark.asyncio
async def test_imported_history_then_template_is_not_planned_twice(
    session_factory,
):
    """Rent imported as plain settled rows Feb-Apr, then a 1500 template due
    May 1: May plans 1500, before AND after the May occurrence is generated
    (after generation the template's frontier is past the period, and it must
    still match its history)."""
    seed = await _seed(session_factory)
    rent = await _category(session_factory, seed, "Rent")
    async with session_factory() as db:
        for month in (2, 3, 4):
            db.add(_settled(seed, rent, 1500, datetime.date(2026, month, 1)))
        await db.commit()
    rid = await _template(
        session_factory, seed, rent, 1500, datetime.date(2026, 5, 1), "Rent",
    )

    [item] = _expense_items(await _populate(session_factory, seed), rent)
    assert item.planned_amount == Decimal("1500.00")
    assert item.source == "recurring"

    from app.services import recurring_service
    async with session_factory() as db:
        await recurring_service.generate_due_transactions(
            db, seed["org_id"], today=datetime.date(2026, 5, 2),
        )
        assert (await db.get(RecurringTransaction, rid)).next_due_date > seed["may_end"]
    async with session_factory() as db:
        resp = await forecast_plan_service.refresh_from_sources(
            db, org_id=seed["org_id"], period_start=seed["may_start"],
        )
    [item] = _expense_items(resp, rent)
    assert item.planned_amount == Decimal("1500.00")


@pytest.mark.asyncio
async def test_promote_to_recurring_leaves_older_rows_out_of_history(
    session_factory,
):
    """Promoting April's rent row links that row only; February and March
    stay NULL and must still be matched out of H'."""
    from app.schemas.transaction import PromoteToRecurringRequest
    from app.services import transaction_service

    seed = await _seed(session_factory)
    rent = await _category(session_factory, seed, "Rent")
    async with session_factory() as db:
        for month in (2, 3):
            db.add(_settled(seed, rent, 1500, datetime.date(2026, month, 1)))
        april = _settled(seed, rent, 1500, datetime.date(2026, 4, 1))
        db.add(april)
        await db.commit()
        april_id = april.id
    async with session_factory() as db:
        await transaction_service.promote_to_recurring(
            db, seed["org_id"], april_id,
            PromoteToRecurringRequest(
                frequency="monthly", next_due_date=datetime.date(2026, 5, 1),
            ),
            today=datetime.date(2026, 5, 2),
        )

    [item] = _expense_items(await _populate(session_factory, seed), rent)
    assert item.planned_amount == Decimal("1500.00")
    assert item.source == "recurring"


@pytest.mark.asyncio
async def test_variable_rows_in_a_template_category_still_count(session_factory):
    """Only rows that look like the template are matched out. A utilities
    row in the same category and account, nowhere near the rent amount,
    stays in H'."""
    seed = await _seed(session_factory)
    housing = await _category(session_factory, seed, "Housing")
    async with session_factory() as db:
        for month, utility in ((2, 180), (3, 200), (4, 220)):
            db.add(_settled(seed, housing, 1500, datetime.date(2026, month, 1)))
            db.add(_settled(seed, housing, utility, datetime.date(2026, month, 15)))
        await db.commit()
    await _template(
        session_factory, seed, housing, 1500, datetime.date(2026, 5, 1), "Rent",
    )

    [item] = _expense_items(await _populate(session_factory, seed), housing)
    assert item.planned_amount == Decimal("1700.00")
    assert item.source == "recurring"


@pytest.mark.parametrize(
    ("feb", "mar", "expected"),
    [
        # Exactly -10% and +10%: matched, H' empty, the template alone.
        ("900.00", "1100.00", "1000.00"),
        # One cent outside on each side: unmatched, H' = 1000.00 on top.
        ("899.99", "1100.01", "2000.00"),
    ],
)
@pytest.mark.asyncio
async def test_template_match_tolerance_is_ten_percent_inclusive(
    session_factory, feb, mar, expected,
):
    seed = await _seed(session_factory)
    loan = await _category(session_factory, seed, "Loan")
    async with session_factory() as db:
        db.add(_settled(seed, loan, feb, datetime.date(2026, 2, 3)))
        db.add(_settled(seed, loan, mar, datetime.date(2026, 3, 3)))
        await db.commit()
    await _template(
        session_factory, seed, loan, 1000, datetime.date(2026, 5, 3), "Loan",
    )

    [item] = _expense_items(await _populate(session_factory, seed), loan)
    assert item.planned_amount == Decimal(expected)


@pytest.mark.asyncio
async def test_template_matches_at_most_its_occurrences_closest_first(
    session_factory,
):
    """A monthly template matches ONE row per history month, the closest in
    amount: the 1000 rows go, the 950 rows (also within 10%) stay in H'."""
    seed = await _seed(session_factory)
    loan = await _category(session_factory, seed, "Loan")
    async with session_factory() as db:
        for month in (2, 3):
            db.add(_settled(seed, loan, 950, datetime.date(2026, month, 2)))
            db.add(_settled(seed, loan, 1000, datetime.date(2026, month, 3)))
        await db.commit()
    await _template(
        session_factory, seed, loan, 1000, datetime.date(2026, 5, 3), "Loan",
    )

    [item] = _expense_items(await _populate(session_factory, seed), loan)
    assert item.planned_amount == Decimal("1950.00")


@pytest.mark.asyncio
async def test_imported_salary_history_then_income_template(session_factory):
    """Income runs the same rule: imported 3000 salary rows plus a 3000
    salary template plan 3000, not 6000."""
    seed = await _seed(session_factory)
    salary = seed["salary_id"]
    async with session_factory() as db:
        for month in (2, 3, 4):
            db.add(_settled(seed, salary, 3000, datetime.date(2026, month, 25),
                            type_=TransactionType.INCOME))
        await db.commit()
    await _template(
        session_factory, seed, salary, 3000, datetime.date(2026, 5, 25),
        "Salary", type_="income",
    )

    resp = await _populate(session_factory, seed)
    [item] = [i for i in resp.items if i.category_id == salary and i.type == "income"]
    assert item.planned_amount == Decimal("3000.00")
    assert item.source == "recurring"


@pytest.mark.asyncio
async def test_unmatched_bank_rows_in_history_are_matched_out(session_factory):
    """Past months where the bank row was never matched to the generated
    occurrence hold BOTH: a pending generated row (out of H' as generated and
    as pending) and a plain settled bank row, which the template match now
    keeps out of H' too. May (occurrence generated, no bank row yet) = 1500."""
    seed = await _seed(session_factory)
    rent = await _category(session_factory, seed, "Rent")
    rid = await _template(
        session_factory, seed, rent, 1500, datetime.date(2026, 6, 1), "Rent",
    )
    async with session_factory() as db:
        for month in (2, 3, 4, 5):
            day = datetime.date(2026, month, 1)
            db.add(_row(seed, rent, 1500, day, recurring_id=rid))
            if month != 5:
                db.add(_settled(seed, rent, 1500, datetime.date(2026, month, 2)))
        await db.commit()

    [item] = _expense_items(await _populate(session_factory, seed), rent)
    assert item.planned_amount == Decimal("1500.00")


@pytest.mark.asyncio
async def test_known_gap_unmatched_bank_row_next_to_this_periods_occurrence(
    session_factory,
):
    """KNOWN GAP, pinned (not desired): in the period being planned, a plain
    settled bank row next to the generated pending occurrence it should have
    been matched to counts in B' AND in R, so the group plans 3000. Same as
    ``forecast_service`` today; reconciling the pair is what fixes it."""
    seed = await _seed(session_factory)
    rent = await _category(session_factory, seed, "Rent")
    rid = await _template(
        session_factory, seed, rent, 1500, datetime.date(2026, 6, 1), "Rent",
    )
    async with session_factory() as db:
        db.add(_row(seed, rent, 1500, datetime.date(2026, 5, 1), recurring_id=rid))
        db.add(_settled(seed, rent, 1500, datetime.date(2026, 5, 2)))
        await db.commit()

    [item] = _expense_items(await _populate(session_factory, seed), rent)
    assert item.planned_amount == Decimal("3000.00")


@pytest.mark.asyncio
async def test_paused_template_with_its_own_history_plans_nothing(
    session_factory,
):
    """Paused = intentionally not planned. Its generated history keeps
    recurring_id, so it stays out of H', and a paused template projects
    nothing, so no item. (Stopping or deleting the template NULLs
    recurring_id, and the history counts again.)"""
    seed = await _seed(session_factory)
    gym = await _category(session_factory, seed, "Gym")
    rid = await _template(
        session_factory, seed, gym, 40, datetime.date(2026, 5, 10), "Gym",
        is_active=False,
    )
    async with session_factory() as db:
        for month in (2, 3, 4):
            db.add(_settled(seed, gym, 40, datetime.date(2026, month, 10),
                            recurring_id=rid))
        await db.commit()

    assert _expense_items(await _populate(session_factory, seed), gym) == []


@pytest.mark.parametrize(
    ("frequency", "year", "month", "expected"),
    [
        (Frequency.MONTHLY, 2026, 2, 1),
        (Frequency.WEEKLY, 2026, 2, 4),     # Mondays: 2, 9, 16, 23
        (Frequency.WEEKLY, 2026, 3, 5),     # Mondays: 2, 9, 16, 23, 30
        (Frequency.BIWEEKLY, 2026, 3, 2),   # Mar 9, Mar 23 (phase of May 4)
        (Frequency.QUARTERLY, 2026, 2, 1),  # Feb, May, Aug, Nov
        (Frequency.QUARTERLY, 2026, 3, 0),
        (Frequency.YEARLY, 2025, 5, 1),
        (Frequency.YEARLY, 2026, 4, 0),
    ],
)
def test_template_occurrences_in_month_caps_the_match(frequency, year, month, expected):
    """The per-month cap is aligned on the template's frontier (Mon 4 May
    2026): weekday, 14-day phase, quarter month, anniversary month."""
    from types import SimpleNamespace

    tpl = SimpleNamespace(next_due_date=datetime.date(2026, 5, 4), frequency=frequency)
    assert forecast_plan_service._template_occurrences_in_month(
        tpl, year, month
    ) == expected
