"""Server-side guard: forecast_plan_service.upsert_item / bulk_upsert
reject items whose resolved category type disagrees with the item's type.
Closes the HIGH finding from PR #144 review (forecast plan upsert doesn't
validate category/type compatibility).
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.account import AccountType
from app.models.billing import BillingPeriod
from app.models.category import Category, CategoryType
from app.models.forecast_plan import ForecastPlan, PlanStatus
from app.models.user import Organization
from app.schemas.forecast_plan import (
    BulkUpsertItem,
    BulkUpsertRequest,
    ForecastPlanItemCreate,
)
from app.services import forecast_plan_service
from app.services.exceptions import ValidationError


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


async def _seed(factory) -> dict:
    """Org + period + draft plan + EXPENSE master + INCOME master + BOTH
    master. Plan is empty so each test starts fresh."""
    org_id = 1
    period_start = datetime.date(2026, 5, 1)
    period_end = datetime.date(2026, 5, 31)

    async with factory() as db:
        db.add(Organization(id=org_id, name="org", billing_cycle_day=1))
        await db.commit()

        # AccountType is unused for plan tests but satisfies FK constraints
        # if any related tests share the seed.
        at = AccountType(
            org_id=org_id, name="Cash", slug="cash", is_system=True
        )
        db.add(at)
        await db.commit()

        expense_master = Category(
            org_id=org_id, name="Groceries", slug="groceries",
            type=CategoryType.EXPENSE,
        )
        income_master = Category(
            org_id=org_id, name="Salary", slug="salary",
            type=CategoryType.INCOME,
        )
        both_master = Category(
            org_id=org_id, name="Transfer", slug="transfer",
            type=CategoryType.BOTH, is_system=True,
        )
        db.add_all([expense_master, income_master, both_master])
        await db.commit()

        period = BillingPeriod(
            org_id=org_id, start_date=period_start, end_date=period_end
        )
        db.add(period)
        await db.commit()

        plan = ForecastPlan(
            org_id=org_id, billing_period_id=period.id,
            status=PlanStatus.DRAFT,
        )
        db.add(plan)
        await db.commit()

        return {
            "org_id": org_id,
            "plan_id": plan.id,
            "expense_master_id": expense_master.id,
            "income_master_id": income_master.id,
            "both_master_id": both_master.id,
        }


# ── upsert_item ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_rejects_income_with_expense_category(session_factory):
    seed = await _seed(session_factory)
    body = ForecastPlanItemCreate(
        category_id=seed["expense_master_id"],
        type="income",
        planned_amount=Decimal("100"),
    )
    async with session_factory() as db:
        with pytest.raises(ValidationError):
            await forecast_plan_service.upsert_item(
                db, seed["org_id"], seed["plan_id"], body
            )


@pytest.mark.asyncio
async def test_upsert_rejects_expense_with_income_category(session_factory):
    seed = await _seed(session_factory)
    body = ForecastPlanItemCreate(
        category_id=seed["income_master_id"],
        type="expense",
        planned_amount=Decimal("100"),
    )
    async with session_factory() as db:
        with pytest.raises(ValidationError):
            await forecast_plan_service.upsert_item(
                db, seed["org_id"], seed["plan_id"], body
            )


@pytest.mark.asyncio
async def test_upsert_accepts_matching_category(session_factory):
    seed = await _seed(session_factory)
    body = ForecastPlanItemCreate(
        category_id=seed["expense_master_id"],
        type="expense",
        planned_amount=Decimal("100"),
    )
    async with session_factory() as db:
        resp = await forecast_plan_service.upsert_item(
            db, seed["org_id"], seed["plan_id"], body
        )
    assert any(
        item.category_id == seed["expense_master_id"] for item in resp.items
    )


@pytest.mark.asyncio
async def test_upsert_accepts_both_category_for_either_type(session_factory):
    """CategoryType.BOTH master accepts either income or expense items."""
    seed = await _seed(session_factory)
    expense_body = ForecastPlanItemCreate(
        category_id=seed["both_master_id"],
        type="expense",
        planned_amount=Decimal("50"),
    )
    income_body = ForecastPlanItemCreate(
        category_id=seed["both_master_id"],
        type="income",
        planned_amount=Decimal("50"),
    )
    async with session_factory() as db:
        await forecast_plan_service.upsert_item(
            db, seed["org_id"], seed["plan_id"], expense_body
        )
        # Same master + different type is allowed because the unique
        # constraint is (plan_id, category_id, type).
        await forecast_plan_service.upsert_item(
            db, seed["org_id"], seed["plan_id"], income_body
        )


# ── bulk_upsert ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bulk_upsert_rejects_when_any_row_mismatches(session_factory):
    """The codebase convention for bulk_upsert is atomic validation
    (existing test pattern: invalid IDs raise before any insert). The
    type-compat guard follows the same pattern: any mismatched row causes
    the whole batch to reject."""
    seed = await _seed(session_factory)
    body = BulkUpsertRequest(
        items=[
            BulkUpsertItem(
                category_id=seed["expense_master_id"],
                type="expense",
                planned_amount=Decimal("100"),
            ),
            BulkUpsertItem(
                category_id=seed["income_master_id"],
                type="expense",  # mismatched
                planned_amount=Decimal("50"),
            ),
        ]
    )
    async with session_factory() as db:
        with pytest.raises(ValidationError):
            await forecast_plan_service.bulk_upsert(
                db, seed["org_id"], seed["plan_id"], body
            )

    # The first row must NOT have been inserted because the batch was
    # rejected atomically.
    async with session_factory() as db:
        resp = await forecast_plan_service.get_or_create_plan(
            db, seed["org_id"]
        )
        assert resp.items == []


@pytest.mark.asyncio
async def test_bulk_upsert_accepts_all_compatible(session_factory):
    seed = await _seed(session_factory)
    body = BulkUpsertRequest(
        items=[
            BulkUpsertItem(
                category_id=seed["expense_master_id"],
                type="expense",
                planned_amount=Decimal("100"),
            ),
            BulkUpsertItem(
                category_id=seed["income_master_id"],
                type="income",
                planned_amount=Decimal("3000"),
            ),
        ]
    )
    async with session_factory() as db:
        resp = await forecast_plan_service.bulk_upsert(
            db, seed["org_id"], seed["plan_id"], body
        )
    assert len(resp.items) == 2
