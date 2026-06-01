"""Subcategory-level forecast items + per-org build-granularity preference.

Covers spec 2026-06-01-forecast-subcategory-items.md (R1-R4):

- R1: mode-aware master validation on upsert_item AND bulk_upsert. In
  subcategory mode a sub item is accepted; a master item is rejected. In
  master mode behavior is unchanged (sub rejected, master accepted).
- R3: per-master XOR guard — a master is built by ONE master-level item OR
  by one-or-more subcategory items, never both. Enforced on upsert_item and
  bulk_upsert, both directions, with ConflictError code "mixed_granularity".
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
from app.services.exceptions import ConflictError, ValidationError
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


@pytest.mark.asyncio
async def test_subcategory_mode_rejects_master_upsert(session_factory):
    """In subcategory mode, a master-level item is rejected (must use subs)."""
    seed = await _seed(session_factory, granularity="subcategory")
    body = ForecastPlanItemCreate(
        category_id=seed["groceries_id"], type="expense", planned_amount=Decimal("50"),
    )
    async with session_factory() as db:
        with pytest.raises(ValidationError):
            await forecast_plan_service.upsert_item(
                db, seed["org_id"], seed["plan_id"], body
            )


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


# ── R3: per-master XOR guard ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_guard_rejects_master_when_subs_exist(session_factory):
    """A sub item exists for Groceries; adding the master item must reject."""
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
        with pytest.raises(ConflictError) as exc:
            await forecast_plan_service.upsert_item(
                db, seed["org_id"], seed["plan_id"],
                ForecastPlanItemCreate(
                    category_id=seed["groceries_id"], type="expense",
                    planned_amount=Decimal("500"),
                ),
            )
        assert exc.value.code == "mixed_granularity"


@pytest.mark.asyncio
async def test_guard_rejects_sub_when_master_exists(session_factory):
    """A master item exists for Groceries; adding a sub must reject."""
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
        with pytest.raises(ConflictError) as exc:
            await forecast_plan_service.upsert_item(
                db, seed["org_id"], seed["plan_id"],
                ForecastPlanItemCreate(
                    category_id=seed["supermarket_id"], type="expense",
                    planned_amount=Decimal("50"),
                ),
            )
        assert exc.value.code == "mixed_granularity"


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
async def test_bulk_guard_rejects_master_plus_sub_same_request(session_factory):
    """A single bulk request mixing a master + its sub (same type) rejects."""
    seed = await _seed(session_factory, granularity="subcategory")
    body = BulkUpsertRequest(items=[
        BulkUpsertItem(
            category_id=seed["groceries_id"], type="expense",
            planned_amount=Decimal("500"),
        ),
        BulkUpsertItem(
            category_id=seed["supermarket_id"], type="expense",
            planned_amount=Decimal("200"),
        ),
    ])
    async with session_factory() as db:
        with pytest.raises((ConflictError, ValidationError)):
            await forecast_plan_service.bulk_upsert(
                db, seed["org_id"], seed["plan_id"], body
            )


@pytest.mark.asyncio
async def test_bulk_guard_rejects_sub_when_master_already_persisted(session_factory):
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
        with pytest.raises(ConflictError):
            await forecast_plan_service.bulk_upsert(
                db, seed["org_id"], seed["plan_id"], body
            )


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


# ── R2/R3 on copy_from_period: XOR guard skips conflicting source items ─────


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
async def test_copy_skips_master_when_target_has_subs(session_factory):
    """Source (May) has a master-level Groceries item; target (June) already
    has a manual Supermarket sub. Copying must SKIP the master item so the
    target never mixes master+sub for Groceries/expense."""
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
    assert seed["groceries_id"] not in cat_ids  # conflicting master skipped


@pytest.mark.asyncio
async def test_copy_skips_sub_when_target_has_master(session_factory):
    """Source (May) has subcategory items for Groceries; target (June)
    already has a master-level Groceries item. Copying must SKIP the subs."""
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
    assert seed["supermarket_id"] not in cat_ids  # conflicting sub skipped
