"""PR #144 #2: source is now server-controlled on public writes.

The reviewer flagged that ``ForecastPlanItemCreate`` and ``BulkUpsertItem``
accepted a client-supplied ``source`` field which then made its way into the
database. Combined with ``refresh_from_sources`` deleting every non-MANUAL
item, a malicious or careless caller posting ``source="history"`` could
make a manually-added line vanish on next refresh.

Fix: drop ``source`` from the public write schemas and pin every public
write path to ``ItemSource.MANUAL``. Internal pipelines (populate, refresh,
copy) keep the right to write RECURRING / HISTORY since they're not
reachable from a public POST body.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
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
    org_id = 1
    period_start = datetime.date(2026, 5, 1)
    period_end = datetime.date(2026, 5, 31)

    async with factory() as db:
        db.add(Organization(id=org_id, name="org", billing_cycle_day=1))
        await db.commit()

        groceries = Category(
            org_id=org_id, name="Groceries", slug="groceries",
            type=CategoryType.EXPENSE,
        )
        salary = Category(
            org_id=org_id, name="Salary", slug="salary",
            type=CategoryType.INCOME,
        )
        db.add_all([groceries, salary])
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
            "groceries_id": groceries.id,
            "salary_id": salary.id,
        }


def test_create_schema_does_not_accept_source():
    """The schema no longer carries a ``source`` field."""
    assert "source" not in ForecastPlanItemCreate.model_fields
    assert "source" not in BulkUpsertItem.model_fields


@pytest.mark.asyncio
async def test_upsert_item_pins_source_to_manual(session_factory):
    seed = await _seed(session_factory)
    body = ForecastPlanItemCreate(
        category_id=seed["groceries_id"],
        type="expense",
        planned_amount=Decimal("100"),
    )
    async with session_factory() as db:
        resp = await forecast_plan_service.upsert_item(
            db, seed["org_id"], seed["plan_id"], body
        )
    item = next(
        i for i in resp.items if i.category_id == seed["groceries_id"]
    )
    assert item.source == "manual"


@pytest.mark.asyncio
async def test_bulk_upsert_pins_source_to_manual(session_factory):
    seed = await _seed(session_factory)
    body = BulkUpsertRequest(items=[
        BulkUpsertItem(
            category_id=seed["groceries_id"],
            type="expense",
            planned_amount=Decimal("100"),
        ),
        BulkUpsertItem(
            category_id=seed["salary_id"],
            type="income",
            planned_amount=Decimal("3000"),
        ),
    ])
    async with session_factory() as db:
        resp = await forecast_plan_service.bulk_upsert(
            db, seed["org_id"], seed["plan_id"], body
        )
    assert all(i.source == "manual" for i in resp.items)


@pytest.mark.asyncio
async def test_upsert_item_silently_ignores_source_in_payload(session_factory):
    """Even if a client somehow sends ``source`` in the JSON body (for
    example via a stale client), Pydantic's default extra-allow drops it
    silently and the service still pins MANUAL. Regression guard."""
    seed = await _seed(session_factory)
    # Build the model from a dict with an extra field; default extra="ignore".
    body = ForecastPlanItemCreate.model_validate({
        "category_id": seed["groceries_id"],
        "type": "expense",
        "planned_amount": "100",
        "source": "history",
    })
    async with session_factory() as db:
        resp = await forecast_plan_service.upsert_item(
            db, seed["org_id"], seed["plan_id"], body
        )
    item = next(
        i for i in resp.items if i.category_id == seed["groceries_id"]
    )
    assert item.source == "manual"
