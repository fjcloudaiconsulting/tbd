"""Next-period budgeting seeds — copy-from-period.

Covers ``budget_service.copy_budgets_from_period``: bulk-seed a target
(next) period from a source period's budgets, skip-existing / idempotent,
and refuse an empty source. Offline: in-memory sqlite, no dispatch.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.billing import BillingPeriod
from app.models.budget import Budget
from app.models.category import Category, CategoryType
from app.models.user import Organization
from app.services import budget_service
from app.services.billing_service import ensure_future_periods
from app.services.exceptions import ValidationError

NEXT_START = datetime.date(2026, 5, 1)
SOURCE_START = datetime.date(2026, 4, 1)


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


async def _seed(factory, *, with_source_budgets: bool) -> dict:
    """Org + open source period (2026-04-01) + 2 master expense cats.
    Optionally seed source-period budgets for both cats."""
    org_id = 1
    async with factory() as db:
        db.add(Organization(id=org_id, name="org", billing_cycle_day=1))
        await db.commit()
        db.add(BillingPeriod(org_id=org_id, start_date=SOURCE_START, end_date=None))
        await db.commit()
        groceries = Category(org_id=org_id, name="Groceries", slug="g", type=CategoryType.EXPENSE)
        dining = Category(org_id=org_id, name="Dining", slug="d", type=CategoryType.EXPENSE)
        db.add_all([groceries, dining])
        await db.commit()
        cats = {"groceries": groceries.id, "dining": dining.id}
        if with_source_budgets:
            db.add_all([
                Budget(org_id=org_id, category_id=cats["groceries"],
                       amount=Decimal("400.00"), period_start=SOURCE_START, period_end=None),
                Budget(org_id=org_id, category_id=cats["dining"],
                       amount=Decimal("200.00"), period_start=SOURCE_START, period_end=None),
            ])
            await db.commit()
        return {"org_id": org_id, "cats": cats}


@pytest.mark.asyncio
async def test_copy_from_period_seeds_target_and_is_idempotent(session_factory):
    seed = await _seed(session_factory, with_source_budgets=True)
    org_id = seed["org_id"]

    async with session_factory() as db:
        await ensure_future_periods(db, org_id)
        next_bp = await db.scalar(
            select(BillingPeriod).where(
                BillingPeriod.org_id == org_id,
                BillingPeriod.start_date == NEXT_START,
            )
        )
        assert next_bp is not None

    async with session_factory() as db:
        out = await budget_service.copy_budgets_from_period(
            db, org_id, source_period_start=SOURCE_START, target_period_start=NEXT_START,
        )
    amounts = {b.category_id: b.amount for b in out}
    assert amounts[seed["cats"]["groceries"]] == Decimal("400.00")
    assert amounts[seed["cats"]["dining"]] == Decimal("200.00")
    assert all(b.period_start == NEXT_START for b in out)

    # Idempotent: a second copy is a no-op (skip-existing, no duplicate rows).
    async with session_factory() as db:
        again = await budget_service.copy_budgets_from_period(
            db, org_id, source_period_start=SOURCE_START, target_period_start=NEXT_START,
        )
    assert len(again) == 2
    async with session_factory() as db:
        rows = (await db.execute(
            select(Budget).where(Budget.period_start == NEXT_START)
        )).scalars().all()
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_copy_from_empty_source_raises(session_factory):
    seed = await _seed(session_factory, with_source_budgets=False)
    org_id = seed["org_id"]
    async with session_factory() as db:
        await ensure_future_periods(db, org_id)
    async with session_factory() as db:
        with pytest.raises(ValidationError):
            await budget_service.copy_budgets_from_period(
                db, org_id, source_period_start=SOURCE_START, target_period_start=NEXT_START,
            )
