from __future__ import annotations

import datetime
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.user import Organization
from app.services.scheduler import audit as sched_audit


@pytest_asyncio.fixture
async def session_factory(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    # record_run/record_reminder open their own session via app.database.async_session
    monkeypatch.setattr(sched_audit, "async_session", factory)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def org(session_factory):
    async with session_factory() as db:
        o = Organization(name="Acme", billing_cycle_day=1)
        db.add(o)
        await db.commit()
        await db.refresh(o)
        return o


async def test_record_run_writes_scheduler_event(session_factory, org):
    await sched_audit.record_run(
        job_type="recurring_generation", outcome="success", org=org,
        detail={"generated": 3},
    )
    async with session_factory() as db:
        rows = (await db.execute(select(AuditEvent))).scalars().all()
    assert len(rows) == 1
    assert rows[0].event_type == "scheduler.recurring_generation.success"
    assert rows[0].actor_email == "system"
    assert rows[0].actor_user_id is None
    assert rows[0].target_org_id == org.id
    assert rows[0].detail == {"generated": 3}


async def test_reminder_dedup(session_factory, org):
    period = datetime.date(2026, 8, 1)
    assert await _sent(session_factory, org, period) is False
    await sched_audit.record_reminder(org=org, period_start=period, detail={})
    assert await _sent(session_factory, org, period) is True
    # a different period is independent
    assert await _sent(session_factory, org, datetime.date(2026, 9, 1)) is False


async def _sent(session_factory, org, period):
    async with session_factory() as db:
        return await sched_audit.reminder_already_sent(db, org.id, period)
