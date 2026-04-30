"""Tests for forecast_plan_service.get_plan_for_period — pins the
read-only behavior the Dashboard relies on. The function must never
auto-create a draft, and must hand back the existing plan with items
when one is present.
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
from app.models.forecast_plan import (
    ForecastItemType,
    ForecastPlan,
    ForecastPlanItem,
    ItemSource,
    PlanStatus,
)
from app.models.user import Organization
from app.services import forecast_plan_service


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


async def _seed_org_and_period(factory: async_sessionmaker[AsyncSession]) -> dict:
    org_id = 1
    start = datetime.date(2026, 4, 1)
    async with factory() as db:
        db.add(Organization(id=org_id, name="org", billing_cycle_day=1))
        await db.commit()
        bp = BillingPeriod(org_id=org_id, start_date=start, end_date=None)
        db.add(bp)
        await db.commit()
        return {"org_id": org_id, "period_start": start, "period_id": bp.id}


@pytest.mark.asyncio
async def test_returns_none_when_no_plan_exists(session_factory):
    seed = await _seed_org_and_period(session_factory)

    async with session_factory() as db:
        result = await forecast_plan_service.get_plan_for_period(
            db, org_id=seed["org_id"], period_start=seed["period_start"],
        )
    assert result is None


@pytest.mark.asyncio
async def test_does_not_auto_create_plan_on_read(session_factory):
    """Calling get_plan_for_period must not insert any forecast_plans rows.

    The whole point of this function vs get_or_create_plan is that
    Dashboard reads stay side-effect-free.
    """
    seed = await _seed_org_and_period(session_factory)

    async with session_factory() as db:
        for _ in range(3):
            await forecast_plan_service.get_plan_for_period(
                db, org_id=seed["org_id"], period_start=seed["period_start"],
            )

    async with session_factory() as db:
        rows = (await db.execute(select(ForecastPlan))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_returns_plan_with_items_when_present(session_factory):
    seed = await _seed_org_and_period(session_factory)

    async with session_factory() as db:
        master = Category(
            org_id=seed["org_id"], name="Groceries",
            slug="groceries", type=CategoryType.EXPENSE,
        )
        db.add(master)
        await db.commit()

        plan = ForecastPlan(
            org_id=seed["org_id"], billing_period_id=seed["period_id"],
            status=PlanStatus.DRAFT,
        )
        db.add(plan)
        await db.commit()
        item = ForecastPlanItem(
            plan_id=plan.id, org_id=seed["org_id"], category_id=master.id,
            type=ForecastItemType.EXPENSE, planned_amount=Decimal("500"),
            source=ItemSource.MANUAL,
        )
        db.add(item)
        await db.commit()

    async with session_factory() as db:
        result = await forecast_plan_service.get_plan_for_period(
            db, org_id=seed["org_id"], period_start=seed["period_start"],
        )

    assert result is not None
    assert result.status == "draft"
    assert result.period_start == seed["period_start"]
    assert len(result.items) == 1
    assert result.items[0].category_name == "Groceries"
    assert result.items[0].planned_amount == Decimal("500")
    assert result.items[0].actual_amount == Decimal("0")
    assert result.total_planned_expense == Decimal("500")
