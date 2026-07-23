"""Tests for the per-org CC statement alert scheduler settings (toggle +
clamped lead-days), mirroring test_scheduler_org_settings.py's fixtures.
"""
from __future__ import annotations

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


async def test_cc_toggle_defaults_on(session_factory, org):
    async with session_factory() as db:
        assert await so.get_bool(db, org.id, so.AUTOMATE_CC_STATEMENT_KEY) is True


async def test_cc_lead_days_default_and_clamp(session_factory, org):
    async with session_factory() as db:
        assert await so.get_cc_statement_lead_days(db, org.id) == 2
        await so.set_value(db, org.id, so.CC_STATEMENT_REMINDER_LEAD_DAYS_KEY, "99")
        await db.commit()
    async with session_factory() as db:
        assert await so.get_cc_statement_lead_days(db, org.id) == 31  # clamped


async def test_cc_lead_days_clamped_on_garbage(session_factory, org):
    async with session_factory() as db:
        await so.set_value(db, org.id, so.CC_STATEMENT_REMINDER_LEAD_DAYS_KEY, "not-a-number")
        await db.commit()
    async with session_factory() as db:
        assert await so.get_cc_statement_lead_days(db, org.id) == 2


async def test_cc_fields_in_get_all(session_factory, org):
    async with session_factory() as db:
        allv = await so.get_all(db, org.id)
        assert allv["automate_cc_statement_alerts"] is True
        assert allv["cc_statement_reminder_lead_days"] == 2
