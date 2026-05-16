"""PR #144 #2: source is now server-controlled on public writes.

The reviewer flagged that ``ForecastPlanItemCreate`` and ``BulkUpsertItem``
accepted a client-supplied ``source`` field which then made its way into the
database. Combined with ``refresh_from_sources`` deleting every non-MANUAL
item, a malicious or careless caller posting ``source="history"`` could
make a manually-added line vanish on next refresh.

Fix: drop ``source`` from the public write schemas and pin every public
write path to ``ItemSource.MANUAL``. Internal pipelines (``populate``,
``refresh``) keep the right to write RECURRING / HISTORY since they're
not reachable from a public POST body — but ``copy_from_period`` IS a
user-initiated public write, so it must also pin MANUAL on the copied
items (L3.11 residual cleanup, 2026-05-16, PR #294). Otherwise items
copied from an auto-populated source period inherit RECURRING/HISTORY
and get silently wiped by the next ``refresh_from_sources`` on the
target period.
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
from app.models.forecast_plan import (
    ForecastItemType,
    ForecastPlan,
    ForecastPlanItem,
    ItemSource,
    PlanStatus,
)
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


# ── copy_from_period: the L3.11 residual cleanup ─────────────────────────────


async def _seed_source_period_with_auto_items(factory) -> dict:
    """Seed:
      - source period (April) with a plan whose items have source=RECURRING
        and source=HISTORY — i.e. what ``populate_from_sources`` would have
        produced.
      - target period (May) with no plan yet (the copy will create one).

    The source-side items represent "leftover auto-populated items" — the
    canonical case where the L3.11 bug bites: copy them forward, refresh
    the target, watch them silently disappear.
    """
    org_id = 1
    source_start = datetime.date(2026, 4, 1)
    source_end = datetime.date(2026, 4, 30)
    target_start = datetime.date(2026, 5, 1)
    target_end = datetime.date(2026, 5, 31)

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

        source_period = BillingPeriod(
            org_id=org_id, start_date=source_start, end_date=source_end
        )
        target_period = BillingPeriod(
            org_id=org_id, start_date=target_start, end_date=target_end
        )
        db.add_all([source_period, target_period])
        await db.commit()

        source_plan = ForecastPlan(
            org_id=org_id, billing_period_id=source_period.id,
            status=PlanStatus.DRAFT,
        )
        db.add(source_plan)
        await db.commit()

        # Auto-populated style items — exactly what populate_from_sources
        # would have written.
        db.add_all([
            ForecastPlanItem(
                plan_id=source_plan.id,
                org_id=org_id,
                category_id=groceries.id,
                type=ForecastItemType.EXPENSE,
                planned_amount=Decimal("400"),
                source=ItemSource.HISTORY,
            ),
            ForecastPlanItem(
                plan_id=source_plan.id,
                org_id=org_id,
                category_id=salary.id,
                type=ForecastItemType.INCOME,
                planned_amount=Decimal("3000"),
                source=ItemSource.RECURRING,
            ),
        ])
        await db.commit()

        return {
            "org_id": org_id,
            "groceries_id": groceries.id,
            "salary_id": salary.id,
            "source_start": source_start,
            "target_start": target_start,
        }


@pytest.mark.asyncio
async def test_copy_from_period_pins_source_to_manual(session_factory):
    """``copy_from_period`` is a user-initiated public write. The pre-fix
    code propagated ``src_item.source`` verbatim, so items copied from an
    auto-populated source plan landed with ``source=RECURRING`` or
    ``source=HISTORY``. The fix pins ``ItemSource.MANUAL`` on every
    copied row. L3.11 residual cleanup."""
    seed = await _seed_source_period_with_auto_items(session_factory)
    async with session_factory() as db:
        resp = await forecast_plan_service.copy_from_period(
            db,
            seed["org_id"],
            target_period_start=seed["target_start"],
            source_period_start=seed["source_start"],
        )

    # Both items copied across.
    by_cat = {item.category_id: item for item in resp.items}
    assert seed["groceries_id"] in by_cat
    assert seed["salary_id"] in by_cat

    # The load-bearing invariant: every copied item is MANUAL,
    # regardless of what the source-period item's source was.
    assert by_cat[seed["groceries_id"]].source == "manual"
    assert by_cat[seed["salary_id"]].source == "manual"


@pytest.mark.asyncio
async def test_refresh_after_copy_preserves_copied_items(session_factory):
    """The downstream consequence of the pinning rule: once a user has
    copied a previous period's plan, the resulting items must survive a
    later ``refresh_from_sources`` on the target period. Pre-fix they
    were dropped (refresh deletes every non-MANUAL item), silently
    wiping user-curated state."""
    seed = await _seed_source_period_with_auto_items(session_factory)
    async with session_factory() as db:
        await forecast_plan_service.copy_from_period(
            db,
            seed["org_id"],
            target_period_start=seed["target_start"],
            source_period_start=seed["source_start"],
        )

    # Now refresh the target period. With the fix, the copied items
    # stay (they're MANUAL). Without the fix, they evaporate.
    async with session_factory() as db:
        resp = await forecast_plan_service.refresh_from_sources(
            db, seed["org_id"], period_start=seed["target_start"]
        )

    cats_present = {item.category_id for item in resp.items}
    assert seed["groceries_id"] in cats_present, (
        "groceries was copied; refresh must not silently drop it"
    )
    assert seed["salary_id"] in cats_present, (
        "salary was copied; refresh must not silently drop it"
    )
