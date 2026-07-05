from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.user import Organization
from app.services.scheduler import org_settings as so


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def org(session_factory):
    async with session_factory() as db:
        o = Organization(name="Acme", billing_cycle_day=1)
        db.add(o)
        await db.commit()
        await db.refresh(o)
        return o


async def test_defaults_when_unset(session_factory, org):
    async with session_factory() as db:
        assert await so.get_bool(db, org.id, so.AUTOMATE_RECURRING_KEY) is True
        assert await so.get_bool(db, org.id, so.AUTOMATE_BILLING_KEY) is True
        assert await so.get_reminder_lead_days(db, org.id) == 3


async def test_set_and_read_back(session_factory, org):
    async with session_factory() as db:
        await so.set_value(db, org.id, so.AUTOMATE_BILLING_KEY, "false")
        await so.set_value(db, org.id, so.REMINDER_LEAD_DAYS_KEY, "7")
        await db.commit()
    async with session_factory() as db:
        assert await so.get_bool(db, org.id, so.AUTOMATE_BILLING_KEY) is False
        assert await so.get_reminder_lead_days(db, org.id) == 7
        allv = await so.get_all(db, org.id)
        assert allv == {
            "automate_recurring_generation": True,
            "automate_billing_close": False,
            "billing_close_reminder_lead_days": 7,
        }


async def test_reminder_lead_days_clamped_on_garbage(session_factory, org):
    async with session_factory() as db:
        await so.set_value(db, org.id, so.REMINDER_LEAD_DAYS_KEY, "not-a-number")
        await db.commit()
    async with session_factory() as db:
        assert await so.get_reminder_lead_days(db, org.id) == 3
