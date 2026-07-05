from __future__ import annotations

import datetime
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.user import Organization
from app.services.scheduler.base import OUTCOME_NOOP, OUTCOME_SUCCESS
from app.services.scheduler.jobs.recurring_generation import RecurringGenerationJob


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


async def test_not_due_when_no_templates(session_factory, monkeypatch):
    job = RecurringGenerationJob()
    async with session_factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org); await db.commit(); await db.refresh(org)
        assert await job.is_due(db, org, datetime.date(2026, 7, 4)) is False


async def test_run_noop_writes_no_audit_and_no_notify(session_factory, monkeypatch):
    job = RecurringGenerationJob()
    calls = {"audit": 0, "notify": 0}
    monkeypatch.setattr(
        "app.services.scheduler.jobs.recurring_generation.generate_due_transactions",
        _fake_generate(generated=0, settled=0),
    )
    monkeypatch.setattr(
        "app.services.scheduler.jobs.recurring_generation.record_run",
        _counter(calls, "audit"),
    )
    monkeypatch.setattr(
        "app.services.scheduler.jobs.recurring_generation.dispatch_notification_to_org_members",
        _counter(calls, "notify"),
    )
    async with session_factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org); await db.commit(); await db.refresh(org)
        res = await job.run(db, org, datetime.date(2026, 7, 4))
    assert res.outcome == OUTCOME_NOOP
    assert calls == {"audit": 0, "notify": 0}


async def test_run_success_records_and_notifies(session_factory, monkeypatch):
    job = RecurringGenerationJob()
    calls = {"audit": 0, "notify": 0}
    monkeypatch.setattr(
        "app.services.scheduler.jobs.recurring_generation.generate_due_transactions",
        _fake_generate(generated=2, settled=1),
    )
    monkeypatch.setattr(
        "app.services.scheduler.jobs.recurring_generation.record_run",
        _counter(calls, "audit", returns=42),
    )
    monkeypatch.setattr(
        "app.services.scheduler.jobs.recurring_generation.dispatch_notification_to_org_members",
        _counter(calls, "notify"),
    )
    async with session_factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org); await db.commit(); await db.refresh(org)
        res = await job.run(db, org, datetime.date(2026, 7, 4))
    assert res.outcome == OUTCOME_SUCCESS
    assert res.counts == {"generated": 2, "settled": 1, "pending": 0}
    assert calls == {"audit": 1, "notify": 1}


def _fake_generate(*, generated, settled):
    async def _f(db, org_id):
        return {"generated": generated, "settled": settled, "pending": 0,
                "period_end": "2026-07-31"}
    return _f


def _counter(store, key, returns=None):
    async def _f(*a, **k):
        store[key] += 1
        return returns
    return _f
