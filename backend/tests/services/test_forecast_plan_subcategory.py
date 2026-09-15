"""Subcategory-level forecast items + per-org build-granularity preference.

Covers spec 2026-06-01-forecast-subcategory-items.md (R1-R4):

- R1: mode-aware master validation on upsert_item AND bulk_upsert. In
  subcategory mode sub AND master items are accepted (TBD-466). In master
  mode behavior is unchanged (sub rejected, master accepted).
- TBD-466 replaced the R3 "never both" XOR guard: a master's own item and
  its sub items coexist and SUM. Actuals are keyed by (category, type): a
  master's own item takes spend on the master plus spend on subs that have
  no item of that type.
- R2: granularity-aware populate/copy.
- core regression: two subs of the same master both persist and sum into
  the master's planned total.
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
from app.models.settings import OrgSetting
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization
from app.schemas.forecast_plan import (
    BulkUpsertItem,
    BulkUpsertRequest,
    ForecastPlanItemCreate,
)
from app.services import forecast_plan_service
from app.services.exceptions import ValidationError
from app.services.settings_service import FORECAST_INPUT_GRANULARITY_KEY


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


async def _seed(factory, *, granularity: str | None = None) -> dict:
    """Org + period + draft plan. Expense master Groceries with two subs
    (Supermarket, Restaurant) and income master Salary with one sub (Bonus).
    Optionally sets the forecast_input_granularity org setting.
    """
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
            org_id=org_id, account_type_id=at.id, name="Wallet", balance=Decimal("0"),
        )
        db.add(acc)
        await db.commit()

        groceries = Category(
            org_id=org_id, name="Groceries", slug="groceries", type=CategoryType.EXPENSE,
        )
        salary = Category(
            org_id=org_id, name="Salary", slug="salary", type=CategoryType.INCOME,
        )
        db.add_all([groceries, salary])
        await db.commit()

        supermarket = Category(
            org_id=org_id, name="Supermarket", slug="supermarket",
            type=CategoryType.EXPENSE, parent_id=groceries.id,
        )
        restaurant = Category(
            org_id=org_id, name="Restaurant", slug="restaurant",
            type=CategoryType.EXPENSE, parent_id=groceries.id,
        )
        bonus = Category(
            org_id=org_id, name="Bonus", slug="bonus",
            type=CategoryType.INCOME, parent_id=salary.id,
        )
        db.add_all([supermarket, restaurant, bonus])
        await db.commit()

        period = BillingPeriod(org_id=org_id, start_date=may_start, end_date=may_end)
        db.add(period)
        await db.commit()

        plan = ForecastPlan(
            org_id=org_id, billing_period_id=period.id, status=PlanStatus.DRAFT,
        )
        db.add(plan)
        await db.commit()

        if granularity is not None:
            db.add(OrgSetting(
                org_id=org_id, key=FORECAST_INPUT_GRANULARITY_KEY, value=granularity,
            ))
            await db.commit()

        return {
            "org_id": org_id,
            "plan_id": plan.id,
            "account_id": acc.id,
            "period_id": period.id,
            "groceries_id": groceries.id,
            "salary_id": salary.id,
            "supermarket_id": supermarket.id,
            "restaurant_id": restaurant.id,
            "bonus_id": bonus.id,
            "may_start": may_start,
            "may_end": may_end,
        }


# ── R1: mode-aware validation ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_master_mode_rejects_subcategory_upsert(session_factory):
    seed = await _seed(session_factory, granularity="master")
    body = ForecastPlanItemCreate(
        category_id=seed["supermarket_id"], type="expense", planned_amount=Decimal("50"),
    )
    async with session_factory() as db:
        with pytest.raises(ValidationError):
            await forecast_plan_service.upsert_item(
                db, seed["org_id"], seed["plan_id"], body
            )


@pytest.mark.asyncio
async def test_master_mode_default_rejects_subcategory(session_factory):
    """No setting row at all = default master mode = sub rejected."""
    seed = await _seed(session_factory)  # no granularity set
    body = ForecastPlanItemCreate(
        category_id=seed["supermarket_id"], type="expense", planned_amount=Decimal("50"),
    )
    async with session_factory() as db:
        with pytest.raises(ValidationError):
            await forecast_plan_service.upsert_item(
                db, seed["org_id"], seed["plan_id"], body
            )


@pytest.mark.asyncio
async def test_subcategory_mode_accepts_subcategory_upsert(session_factory):
    seed = await _seed(session_factory, granularity="subcategory")
    body = ForecastPlanItemCreate(
        category_id=seed["supermarket_id"], type="expense", planned_amount=Decimal("50"),
    )
    async with session_factory() as db:
        resp = await forecast_plan_service.upsert_item(
            db, seed["org_id"], seed["plan_id"], body
        )
    assert any(i.category_id == seed["supermarket_id"] for i in resp.items)


# ── Core regression: two subs of one master both persist + sum ─────────────


@pytest.mark.asyncio
async def test_two_subs_of_one_master_both_persist_and_sum(session_factory):
    seed = await _seed(session_factory, granularity="subcategory")
    async with session_factory() as db:
        await forecast_plan_service.upsert_item(
            db, seed["org_id"], seed["plan_id"],
            ForecastPlanItemCreate(
                category_id=seed["supermarket_id"], type="expense",
                planned_amount=Decimal("200"),
            ),
        )
    async with session_factory() as db:
        resp = await forecast_plan_service.upsert_item(
            db, seed["org_id"], seed["plan_id"],
            ForecastPlanItemCreate(
                category_id=seed["restaurant_id"], type="expense",
                planned_amount=Decimal("150"),
            ),
        )
    cat_ids = {i.category_id for i in resp.items}
    assert seed["supermarket_id"] in cat_ids
    assert seed["restaurant_id"] in cat_ids
    # The master's planned total = sum of its subs.
    assert resp.total_planned_expense == Decimal("350")


# ── TBD-466: master + sub items coexist across a mode switch ───────────────


@pytest.mark.asyncio
async def test_master_item_accepted_when_leftover_subs_exist(session_factory):
    """A sub item exists for Groceries; after a switch to master mode the
    master item is accepted alongside it and the two sum (TBD-466)."""
    seed = await _seed(session_factory, granularity="subcategory")
    async with session_factory() as db:
        await forecast_plan_service.upsert_item(
            db, seed["org_id"], seed["plan_id"],
            ForecastPlanItemCreate(
                category_id=seed["supermarket_id"], type="expense",
                planned_amount=Decimal("200"),
            ),
        )
    # Now flip to master mode and try to add the master item.
    async with session_factory() as db:
        setting = (await db.execute(
            select(OrgSetting).where(OrgSetting.org_id == seed["org_id"])
        )).scalar_one()
        setting.value = "master"
        await db.commit()
    async with session_factory() as db:
        resp = await forecast_plan_service.upsert_item(
            db, seed["org_id"], seed["plan_id"],
            ForecastPlanItemCreate(
                category_id=seed["groceries_id"], type="expense",
                planned_amount=Decimal("500"),
            ),
        )
    assert {i.category_id for i in resp.items} == {
        seed["groceries_id"], seed["supermarket_id"],
    }
    assert resp.total_planned_expense == Decimal("700")


@pytest.mark.asyncio
async def test_sub_item_accepted_when_master_item_exists(session_factory):
    """A master item exists for Groceries; after a switch to subcategory
    mode a sub item is accepted alongside it (TBD-466)."""
    seed = await _seed(session_factory, granularity="master")
    async with session_factory() as db:
        await forecast_plan_service.upsert_item(
            db, seed["org_id"], seed["plan_id"],
            ForecastPlanItemCreate(
                category_id=seed["groceries_id"], type="expense",
                planned_amount=Decimal("500"),
            ),
        )
    async with session_factory() as db:
        setting = (await db.execute(
            select(OrgSetting).where(OrgSetting.org_id == seed["org_id"])
        )).scalar_one()
        setting.value = "subcategory"
        await db.commit()
    async with session_factory() as db:
        resp = await forecast_plan_service.upsert_item(
            db, seed["org_id"], seed["plan_id"],
            ForecastPlanItemCreate(
                category_id=seed["supermarket_id"], type="expense",
                planned_amount=Decimal("50"),
            ),
        )
    assert {i.category_id for i in resp.items} == {
        seed["groceries_id"], seed["supermarket_id"],
    }
    assert resp.total_planned_expense == Decimal("550")


@pytest.mark.asyncio
async def test_guard_different_type_not_conflicting(session_factory):
    """The guard is per (master, type). A BOTH-typed master could carry an
    income master item and an expense sub item independently — but our seed
    masters are single-typed, so just verify same-master different-type is
    handled per type: add expense sub, then income sub under salary master."""
    seed = await _seed(session_factory, granularity="subcategory")
    async with session_factory() as db:
        await forecast_plan_service.upsert_item(
            db, seed["org_id"], seed["plan_id"],
            ForecastPlanItemCreate(
                category_id=seed["supermarket_id"], type="expense",
                planned_amount=Decimal("200"),
            ),
        )
    async with session_factory() as db:
        resp = await forecast_plan_service.upsert_item(
            db, seed["org_id"], seed["plan_id"],
            ForecastPlanItemCreate(
                category_id=seed["bonus_id"], type="income",
                planned_amount=Decimal("300"),
            ),
        )
    assert resp.total_planned_expense == Decimal("200")
    assert resp.total_planned_income == Decimal("300")


# ── R3 on bulk_upsert ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bulk_subcategory_mode_accepts_subs(session_factory):
    seed = await _seed(session_factory, granularity="subcategory")
    body = BulkUpsertRequest(items=[
        BulkUpsertItem(
            category_id=seed["supermarket_id"], type="expense",
            planned_amount=Decimal("200"),
        ),
        BulkUpsertItem(
            category_id=seed["restaurant_id"], type="expense",
            planned_amount=Decimal("150"),
        ),
    ])
    async with session_factory() as db:
        resp = await forecast_plan_service.bulk_upsert(
            db, seed["org_id"], seed["plan_id"], body
        )
    assert len(resp.items) == 2
    assert resp.total_planned_expense == Decimal("350")


@pytest.mark.asyncio
async def test_bulk_master_mode_rejects_subs(session_factory):
    seed = await _seed(session_factory, granularity="master")
    body = BulkUpsertRequest(items=[
        BulkUpsertItem(
            category_id=seed["supermarket_id"], type="expense",
            planned_amount=Decimal("200"),
        ),
    ])
    async with session_factory() as db:
        with pytest.raises(ValidationError):
            await forecast_plan_service.bulk_upsert(
                db, seed["org_id"], seed["plan_id"], body
            )


@pytest.mark.asyncio
async def test_bulk_accepts_sub_when_master_already_persisted(session_factory):
    seed = await _seed(session_factory, granularity="master")
    async with session_factory() as db:
        await forecast_plan_service.upsert_item(
            db, seed["org_id"], seed["plan_id"],
            ForecastPlanItemCreate(
                category_id=seed["groceries_id"], type="expense",
                planned_amount=Decimal("500"),
            ),
        )
    async with session_factory() as db:
        setting = (await db.execute(
            select(OrgSetting).where(OrgSetting.org_id == seed["org_id"])
        )).scalar_one()
        setting.value = "subcategory"
        await db.commit()
    body = BulkUpsertRequest(items=[
        BulkUpsertItem(
            category_id=seed["supermarket_id"], type="expense",
            planned_amount=Decimal("200"),
        ),
    ])
    async with session_factory() as db:
        resp = await forecast_plan_service.bulk_upsert(
            db, seed["org_id"], seed["plan_id"], body
        )
    assert resp.total_planned_expense == Decimal("700")


# ── R2: granularity-aware populate ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_populate_subcategory_mode_groups_by_sub(session_factory):
    """In subcategory mode, history populate groups by the txn's own
    subcategory, producing per-sub items (not a rolled-up master item)."""
    seed = await _seed(session_factory, granularity="subcategory")
    org_id = seed["org_id"]
    acc_id = seed["account_id"]
    # Two months of settled history tagged to two different subs of Groceries.
    async with session_factory() as db:
        for m in (2, 3):  # Feb, Mar 2026 (within 3-month window before May)
            db.add(Transaction(
                org_id=org_id, account_id=acc_id, category_id=seed["supermarket_id"],
                type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
                amount=Decimal("100"), date=datetime.date(2026, m, 10),
                settled_date=datetime.date(2026, m, 10), description="groc",
            ))
            db.add(Transaction(
                org_id=org_id, account_id=acc_id, category_id=seed["restaurant_id"],
                type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
                amount=Decimal("60"), date=datetime.date(2026, m, 12),
                settled_date=datetime.date(2026, m, 12), description="rest",
            ))
        await db.commit()

    async with session_factory() as db:
        resp = await forecast_plan_service.populate_from_sources(
            db, org_id, period_start=seed["may_start"]
        )
    cat_ids = {i.category_id for i in resp.items}
    assert seed["supermarket_id"] in cat_ids
    assert seed["restaurant_id"] in cat_ids
    # Master id must NOT appear as its own item in subcategory mode.
    assert seed["groceries_id"] not in cat_ids


@pytest.mark.asyncio
async def test_populate_master_mode_rolls_to_master(session_factory):
    """In master mode (default), history populate rolls subs to the master."""
    seed = await _seed(session_factory, granularity="master")
    org_id = seed["org_id"]
    acc_id = seed["account_id"]
    async with session_factory() as db:
        for m in (2, 3):
            db.add(Transaction(
                org_id=org_id, account_id=acc_id, category_id=seed["supermarket_id"],
                type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
                amount=Decimal("100"), date=datetime.date(2026, m, 10),
                settled_date=datetime.date(2026, m, 10), description="groc",
            ))
        await db.commit()

    async with session_factory() as db:
        resp = await forecast_plan_service.populate_from_sources(
            db, org_id, period_start=seed["may_start"]
        )
    cat_ids = {i.category_id for i in resp.items}
    assert seed["groceries_id"] in cat_ids
    assert seed["supermarket_id"] not in cat_ids


@pytest.mark.asyncio
async def test_populate_skips_master_with_existing_manual_subs(session_factory):
    """Guard inside populate: if a manual sub item exists for a master,
    populate (in master mode) must not create a conflicting master item."""
    seed = await _seed(session_factory, granularity="subcategory")
    org_id = seed["org_id"]
    acc_id = seed["account_id"]
    # Manual sub item already on the plan.
    async with session_factory() as db:
        await forecast_plan_service.upsert_item(
            db, org_id, seed["plan_id"],
            ForecastPlanItemCreate(
                category_id=seed["supermarket_id"], type="expense",
                planned_amount=Decimal("200"),
            ),
        )
    # Flip to master mode, then populate from history that would roll to the master.
    async with session_factory() as db:
        setting = (await db.execute(
            select(OrgSetting).where(OrgSetting.org_id == org_id)
        )).scalar_one()
        setting.value = "master"
        await db.commit()
    async with session_factory() as db:
        for m in (2, 3):
            db.add(Transaction(
                org_id=org_id, account_id=acc_id, category_id=seed["restaurant_id"],
                type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
                amount=Decimal("100"), date=datetime.date(2026, m, 10),
                settled_date=datetime.date(2026, m, 10), description="r",
            ))
        await db.commit()
    async with session_factory() as db:
        resp = await forecast_plan_service.populate_from_sources(
            db, org_id, period_start=seed["may_start"]
        )
    cat_ids = {i.category_id for i in resp.items}
    # Manual sub stays; the master must NOT have been added (would conflict).
    assert seed["supermarket_id"] in cat_ids
    assert seed["groceries_id"] not in cat_ids


# ── copy_from_period: master and sub items both carry over (TBD-466) ───────


async def _seed_second_period(factory, seed: dict) -> int:
    """Add a second (June) billing period to the existing org and return its
    start date marker via a created draft plan's period start."""
    org_id = seed["org_id"]
    jun_start = datetime.date(2026, 6, 1)
    jun_end = datetime.date(2026, 6, 30)
    async with factory() as db:
        period = BillingPeriod(org_id=org_id, start_date=jun_start, end_date=jun_end)
        db.add(period)
        await db.commit()
    return jun_start


@pytest.mark.asyncio
async def test_copy_carries_master_when_target_has_subs(session_factory):
    """Source (May) has a master-level Groceries item; target (June) already
    has a manual Supermarket sub. The master item is copied alongside it
    (TBD-466: they sum)."""
    # May plan built in master mode with a master Groceries item.
    seed = await _seed(session_factory, granularity="master")
    org_id = seed["org_id"]
    async with session_factory() as db:
        await forecast_plan_service.upsert_item(
            db, org_id, seed["plan_id"],
            ForecastPlanItemCreate(
                category_id=seed["groceries_id"], type="expense",
                planned_amount=Decimal("500"),
            ),
        )

    jun_start = await _seed_second_period(session_factory, seed)

    # Flip to subcategory mode and seed the June target with a manual sub.
    async with session_factory() as db:
        setting = (await db.execute(
            select(OrgSetting).where(OrgSetting.org_id == org_id)
        )).scalar_one()
        setting.value = "subcategory"
        await db.commit()
    async with session_factory() as db:
        # get_or_create the June plan, then add a sub.
        await forecast_plan_service.get_or_create_plan(
            db, org_id, period_start=jun_start
        )
    async with session_factory() as db:
        jun_plan = (await db.execute(
            select(ForecastPlan).join(BillingPeriod).where(
                ForecastPlan.org_id == org_id,
                BillingPeriod.start_date == jun_start,
            )
        )).scalar_one()
        await forecast_plan_service.upsert_item(
            db, org_id, jun_plan.id,
            ForecastPlanItemCreate(
                category_id=seed["supermarket_id"], type="expense",
                planned_amount=Decimal("200"),
            ),
        )

    # Copy May → June. The master Groceries item must be skipped.
    async with session_factory() as db:
        resp = await forecast_plan_service.copy_from_period(
            db, org_id,
            target_period_start=jun_start,
            source_period_start=seed["may_start"],
        )
    cat_ids = {i.category_id for i in resp.items}
    assert seed["supermarket_id"] in cat_ids  # manual sub stays
    assert seed["groceries_id"] in cat_ids  # master copied alongside it


@pytest.mark.asyncio
async def test_copy_carries_sub_when_target_has_master(session_factory):
    """Source (May) has a subcategory item for Groceries; target (June)
    already has a master-level Groceries item. The sub is copied alongside
    it (TBD-466: they sum)."""
    # May plan built in subcategory mode with two subs.
    seed = await _seed(session_factory, granularity="subcategory")
    org_id = seed["org_id"]
    async with session_factory() as db:
        await forecast_plan_service.upsert_item(
            db, org_id, seed["plan_id"],
            ForecastPlanItemCreate(
                category_id=seed["supermarket_id"], type="expense",
                planned_amount=Decimal("200"),
            ),
        )

    jun_start = await _seed_second_period(session_factory, seed)

    # Flip to master mode and seed June target with a master item.
    async with session_factory() as db:
        setting = (await db.execute(
            select(OrgSetting).where(OrgSetting.org_id == org_id)
        )).scalar_one()
        setting.value = "master"
        await db.commit()
    async with session_factory() as db:
        await forecast_plan_service.get_or_create_plan(
            db, org_id, period_start=jun_start
        )
    async with session_factory() as db:
        jun_plan = (await db.execute(
            select(ForecastPlan).join(BillingPeriod).where(
                ForecastPlan.org_id == org_id,
                BillingPeriod.start_date == jun_start,
            )
        )).scalar_one()
        await forecast_plan_service.upsert_item(
            db, org_id, jun_plan.id,
            ForecastPlanItemCreate(
                category_id=seed["groceries_id"], type="expense",
                planned_amount=Decimal("500"),
            ),
        )

    async with session_factory() as db:
        resp = await forecast_plan_service.copy_from_period(
            db, org_id,
            target_period_start=jun_start,
            source_period_start=seed["may_start"],
        )
    cat_ids = {i.category_id for i in resp.items}
    assert seed["groceries_id"] in cat_ids  # target master stays
    assert seed["supermarket_id"] in cat_ids  # sub copied alongside it


# ── TBD-466: a master's own item and its sub items SUM ─────────────────────
#
# Fixture, asymmetric on purpose (spec 2026-09-14-tbd-466):
#   Master M = Groceries (expense). Subs S = Supermarket, T = Restaurant.
#   S2 = Deli, a BOTH-typed sub of M.
#   Items: M expense 500, S expense 200, S2 INCOME 10. No item on T.
#   Settled May expense spend: M 30, S 70, T 11, S2 50.


async def _seed_sum(factory, *, granularity: str = "subcategory") -> dict:
    seed = await _seed(factory, granularity=granularity)
    async with factory() as db:
        deli = Category(
            org_id=seed["org_id"], name="Deli", slug="deli",
            type=CategoryType.BOTH, parent_id=seed["groceries_id"],
        )
        db.add(deli)
        await db.commit()
        seed["s2_id"] = deli.id
        spend = (
            (seed["groceries_id"], "30"), (seed["supermarket_id"], "70"),
            (seed["restaurant_id"], "11"), (deli.id, "50"),
        )
        for cat_id, amount in spend:
            db.add(Transaction(
                org_id=seed["org_id"], account_id=seed["account_id"],
                category_id=cat_id, type=TransactionType.EXPENSE,
                status=TransactionStatus.SETTLED, amount=Decimal(amount),
                date=datetime.date(2026, 5, 10),
                settled_date=datetime.date(2026, 5, 10), description="spend",
            ))
        await db.commit()
    return seed


async def _add_sum_items(factory, seed: dict) -> None:
    """Persist the fixture's three items straight onto the May plan."""
    rows = (
        (seed["groceries_id"], ForecastItemType.EXPENSE, "500"),
        (seed["supermarket_id"], ForecastItemType.EXPENSE, "200"),
        (seed["s2_id"], ForecastItemType.INCOME, "10"),
    )
    async with factory() as db:
        for cat_id, item_type, amount in rows:
            db.add(ForecastPlanItem(
                plan_id=seed["plan_id"], org_id=seed["org_id"],
                category_id=cat_id, type=item_type,
                planned_amount=Decimal(amount), source=ItemSource.MANUAL,
            ))
        await db.commit()


def _by_key(resp) -> dict:
    return {(i.category_id, i.type): i for i in resp.items}


@pytest.mark.asyncio
async def test_f1_master_item_and_sub_item_sum_in_planned_totals(session_factory):
    """F1: M 500 + S 200 → 700. Kills master-replaces-subs (500) and max."""
    seed = await _seed_sum(session_factory)
    for cat_id, item_type, amount in (
        (seed["supermarket_id"], "expense", "200"),
        (seed["groceries_id"], "expense", "500"),
        (seed["s2_id"], "income", "10"),
    ):
        async with session_factory() as db:
            resp = await forecast_plan_service.upsert_item(
                db, seed["org_id"], seed["plan_id"],
                ForecastPlanItemCreate(
                    category_id=cat_id, type=item_type,
                    planned_amount=Decimal(amount),
                ),
            )
    assert resp.total_planned_expense == Decimal("700")
    m_group = sum(
        i.planned_amount for i in resp.items
        if i.type == "expense"
        and (i.parent_id or i.category_id) == seed["groceries_id"]
    )
    assert m_group == Decimal("700")


@pytest.mark.asyncio
async def test_f2_actuals_each_transaction_lands_in_exactly_one_item(session_factory):
    """F2: actual(M) = 30+11+50 = 91, actual(S) = 70, total 161.
    Kills the loop-swap (M 161 / S 0), a full rollup into M (M 161, total
    231) and master-own-only (M 30)."""
    seed = await _seed_sum(session_factory)
    await _add_sum_items(session_factory, seed)
    async with session_factory() as db:
        resp = await forecast_plan_service.get_or_create_plan(
            db, seed["org_id"], period_start=seed["may_start"]
        )
    items = _by_key(resp)
    assert items[(seed["groceries_id"], "expense")].actual_amount == Decimal("91")
    assert items[(seed["supermarket_id"], "expense")].actual_amount == Decimal("70")
    assert items[(seed["s2_id"], "income")].actual_amount == Decimal("0")
    assert resp.total_actual_expense == Decimal("161")


@pytest.mark.asyncio
async def test_f3_both_typed_sub_expense_lands_in_master_despite_its_income_item(
    session_factory,
):
    """F3: S2 has an INCOME item only, so its expense 50 belongs to M
    (M = 91, not 41). Kills keying on category id alone (type-blind)."""
    seed = await _seed_sum(session_factory)
    await _add_sum_items(session_factory, seed)
    async with session_factory() as db:
        resp = await forecast_plan_service.get_or_create_plan(
            db, seed["org_id"], period_start=seed["may_start"]
        )
    m_actual = _by_key(resp)[(seed["groceries_id"], "expense")].actual_amount
    assert m_actual != Decimal("41")
    assert m_actual == Decimal("91")


@pytest.mark.asyncio
async def test_f4_subcategory_mode_upsert_sub_then_master_both_persist(session_factory):
    """F4 (single): subcategory mode, upsert S then M, both persist. Kills
    the never-both guard and the subcategory-mode rejection of masters."""
    seed = await _seed_sum(session_factory)
    for cat_id, amount in (
        (seed["supermarket_id"], "200"), (seed["groceries_id"], "500"),
    ):
        async with session_factory() as db:
            resp = await forecast_plan_service.upsert_item(
                db, seed["org_id"], seed["plan_id"],
                ForecastPlanItemCreate(
                    category_id=cat_id, type="expense",
                    planned_amount=Decimal(amount),
                ),
            )
    items = _by_key(resp)
    assert items[(seed["supermarket_id"], "expense")].planned_amount == Decimal("200")
    assert items[(seed["groceries_id"], "expense")].planned_amount == Decimal("500")


@pytest.mark.asyncio
async def test_f4_subcategory_mode_bulk_sub_then_master_both_persist(session_factory):
    """F4 (bulk): S persisted by one bulk call; M arrives in the next one
    alongside another sub of the same master."""
    seed = await _seed_sum(session_factory)
    async with session_factory() as db:
        await forecast_plan_service.bulk_upsert(
            db, seed["org_id"], seed["plan_id"],
            BulkUpsertRequest(items=[BulkUpsertItem(
                category_id=seed["supermarket_id"], type="expense",
                planned_amount=Decimal("200"),
            )]),
        )
    async with session_factory() as db:
        resp = await forecast_plan_service.bulk_upsert(
            db, seed["org_id"], seed["plan_id"],
            BulkUpsertRequest(items=[
                BulkUpsertItem(
                    category_id=seed["groceries_id"], type="expense",
                    planned_amount=Decimal("500"),
                ),
                BulkUpsertItem(
                    category_id=seed["restaurant_id"], type="expense",
                    planned_amount=Decimal("40"),
                ),
            ]),
        )
    items = _by_key(resp)
    assert items[(seed["supermarket_id"], "expense")].planned_amount == Decimal("200")
    assert items[(seed["groceries_id"], "expense")].planned_amount == Decimal("500")
    assert items[(seed["restaurant_id"], "expense")].planned_amount == Decimal("40")


@pytest.mark.asyncio
async def test_f5_copy_master_and_sub_into_empty_plan_keeps_both(session_factory):
    """F5: copy_from_period of M+S into an empty plan → both items, 700.
    Kills a leftover copy guard (it silently drops one of them)."""
    seed = await _seed_sum(session_factory)
    await _add_sum_items(session_factory, seed)
    jun_start = await _seed_second_period(session_factory, seed)
    async with session_factory() as db:
        resp = await forecast_plan_service.copy_from_period(
            db, seed["org_id"],
            target_period_start=jun_start,
            source_period_start=seed["may_start"],
        )
    items = _by_key(resp)
    assert (seed["groceries_id"], "expense") in items
    assert (seed["supermarket_id"], "expense") in items
    assert resp.total_planned_expense == Decimal("700")


@pytest.mark.asyncio
async def test_f7_subcategory_mode_populate_creates_master_from_its_own_spend(
    session_factory,
):
    """F7: subcategory mode, S has an item, spend is booked on M itself →
    populate creates an M item from that spend. Kills the populate guard
    kept in subcategory mode."""
    seed = await _seed_sum(session_factory)
    async with session_factory() as db:
        await forecast_plan_service.upsert_item(
            db, seed["org_id"], seed["plan_id"],
            ForecastPlanItemCreate(
                category_id=seed["supermarket_id"], type="expense",
                planned_amount=Decimal("200"),
            ),
        )
    async with session_factory() as db:
        resp = await forecast_plan_service.populate_from_sources(
            db, seed["org_id"], period_start=seed["may_start"]
        )
    items = _by_key(resp)
    assert items[(seed["groceries_id"], "expense")].planned_amount == Decimal("30")
    assert items[(seed["supermarket_id"], "expense")].planned_amount == Decimal("200")
