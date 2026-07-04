from __future__ import annotations

import datetime
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.billing import BillingPeriod
from app.models.user import Organization
from app.services import billing_service
from app.services.scheduler.jobs.billing_close import BillingCloseJob


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _seed(session_factory, cycle_day, period_start):
    async with session_factory() as db:
        org = Organization(name="Acme", billing_cycle_day=cycle_day)
        db.add(org); await db.flush()
        db.add(BillingPeriod(org_id=org.id, start_date=period_start))
        await db.commit(); await db.refresh(org)
        return org


async def test_not_due_before_boundary(session_factory, monkeypatch):
    _silence_side_effects(monkeypatch)
    org = await _seed(session_factory, cycle_day=1, period_start=datetime.date(2026, 7, 1))
    job = BillingCloseJob()
    async with session_factory() as db:
        # today is mid-cycle; boundary (Jul 1) == current start -> not due
        assert await job.is_due(db, org, datetime.date(2026, 7, 15)) is False


async def test_due_when_period_straddles_boundary(session_factory, monkeypatch):
    _silence_side_effects(monkeypatch)
    org = await _seed(session_factory, cycle_day=1, period_start=datetime.date(2026, 7, 1))
    job = BillingCloseJob()
    async with session_factory() as db:
        # today = Aug 3, boundary = Aug 1 > current start (Jul 1) -> due
        assert await job.is_due(db, org, datetime.date(2026, 8, 3)) is True


async def test_run_closes_and_is_idempotent(session_factory, monkeypatch):
    _silence_side_effects(monkeypatch)
    org = await _seed(session_factory, cycle_day=1, period_start=datetime.date(2026, 7, 1))
    job = BillingCloseJob()
    today = datetime.date(2026, 8, 3)
    async with session_factory() as db:
        res = await job.run(db, org, today)
    assert res.outcome == "success"
    # new open period starts on the boundary (Aug 1)
    async with session_factory() as db:
        cur = await billing_service.get_current_period(db, org.id)
        assert cur.start_date == datetime.date(2026, 8, 1)
        assert await job.is_due(db, org, today) is False  # idempotent


def _silence_side_effects(monkeypatch):
    async def _noop_audit(**k):
        return 1
    async def _noop_notify(*a, **k):
        return 0
    monkeypatch.setattr("app.services.scheduler.jobs.billing_close.record_run", _noop_audit)
    monkeypatch.setattr(
        "app.services.scheduler.jobs.billing_close.dispatch_notification_to_org_members", _noop_notify
    )
