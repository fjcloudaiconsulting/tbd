"""Tests for budget_service.create_budgets_from_forecast — pins the
coupling action that lets users seed current-period budgets from
their forecast plan.
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
from app.models.budget import Budget
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
from app.services import budget_service
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


async def _seed(factory: async_sessionmaker[AsyncSession], *, with_plan: bool) -> dict:
    """Org + open billing period + 3 master expense cats + 1 income cat.
    Plan is created with two expense items and one income item when
    `with_plan=True`."""
    org_id = 1
    start = datetime.date(2026, 4, 1)
    async with factory() as db:
        db.add(Organization(id=org_id, name="org", billing_cycle_day=1))
        await db.commit()

        bp = BillingPeriod(org_id=org_id, start_date=start, end_date=None)
        db.add(bp)
        await db.commit()

        groceries = Category(org_id=org_id, name="Groceries", slug="g", type=CategoryType.EXPENSE)
        rent = Category(org_id=org_id, name="Rent", slug="r", type=CategoryType.EXPENSE)
        utilities = Category(org_id=org_id, name="Utilities", slug="u", type=CategoryType.EXPENSE)
        salary = Category(org_id=org_id, name="Salary", slug="s", type=CategoryType.INCOME)
        db.add_all([groceries, rent, utilities, salary])
        await db.commit()

        cat_ids = {
            "groceries": groceries.id, "rent": rent.id,
            "utilities": utilities.id, "salary": salary.id,
        }
        out = {"org_id": org_id, "period_id": bp.id, "period_start": start, "cats": cat_ids}

        if with_plan:
            plan = ForecastPlan(
                org_id=org_id, billing_period_id=bp.id, status=PlanStatus.ACTIVE,
            )
            db.add(plan)
            await db.commit()
            db.add_all([
                ForecastPlanItem(
                    plan_id=plan.id, org_id=org_id, category_id=cat_ids["groceries"],
                    type=ForecastItemType.EXPENSE, planned_amount=Decimal("400"),
                    source=ItemSource.MANUAL,
                ),
                ForecastPlanItem(
                    plan_id=plan.id, org_id=org_id, category_id=cat_ids["rent"],
                    type=ForecastItemType.EXPENSE, planned_amount=Decimal("1500"),
                    source=ItemSource.MANUAL,
                ),
                ForecastPlanItem(
                    plan_id=plan.id, org_id=org_id, category_id=cat_ids["salary"],
                    type=ForecastItemType.INCOME, planned_amount=Decimal("3500"),
                    source=ItemSource.MANUAL,
                ),
            ])
            await db.commit()
            out["plan_id"] = plan.id

        return out


@pytest.mark.asyncio
async def test_raises_when_no_plan_exists(session_factory):
    seed = await _seed(session_factory, with_plan=False)

    async with session_factory() as db:
        with pytest.raises(ValidationError):
            await budget_service.create_budgets_from_forecast(db, seed["org_id"])

    # No partial state
    async with session_factory() as db:
        rows = (await db.execute(select(Budget))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_copies_expense_items_skips_income(session_factory):
    seed = await _seed(session_factory, with_plan=True)

    async with session_factory() as db:
        result = await budget_service.create_budgets_from_forecast(db, seed["org_id"])

    # Only expense items become budgets — salary income is dropped.
    assert len(result) == 2
    by_cat = {r.category_id: r for r in result}
    assert by_cat[seed["cats"]["groceries"]].amount == Decimal("400")
    assert by_cat[seed["cats"]["rent"]].amount == Decimal("1500")
    assert seed["cats"]["salary"] not in by_cat


@pytest.mark.asyncio
async def test_skips_categories_that_already_have_a_budget(session_factory):
    seed = await _seed(session_factory, with_plan=True)

    # Pre-existing budget for groceries with a custom amount.
    async with session_factory() as db:
        db.add(Budget(
            org_id=seed["org_id"], category_id=seed["cats"]["groceries"],
            amount=Decimal("250"),  # different from plan's 400
            period_start=seed["period_start"], period_end=None,
        ))
        await db.commit()

    async with session_factory() as db:
        result = await budget_service.create_budgets_from_forecast(db, seed["org_id"])

    assert len(result) == 2
    by_cat = {r.category_id: r for r in result}
    # Existing budget preserved with its custom amount, NOT overwritten by the plan.
    assert by_cat[seed["cats"]["groceries"]].amount == Decimal("250")
    # Rent newly created from the plan.
    assert by_cat[seed["cats"]["rent"]].amount == Decimal("1500")


@pytest.mark.asyncio
async def test_idempotent_on_repeat_call(session_factory):
    seed = await _seed(session_factory, with_plan=True)

    async with session_factory() as db:
        first = await budget_service.create_budgets_from_forecast(db, seed["org_id"])
    async with session_factory() as db:
        second = await budget_service.create_budgets_from_forecast(db, seed["org_id"])

    # Same set of budgets after both calls. Same row IDs => no new rows on call 2.
    assert {b.id for b in first} == {b.id for b in second}
    assert len(first) == 2
