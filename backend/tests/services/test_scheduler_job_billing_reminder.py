from __future__ import annotations

import datetime
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.billing import BillingPeriod
from app.models.user import Organization
from app.services.scheduler.jobs.billing_reminder import BillingReminderJob


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


async def _seed(session_factory, cycle_day=1, period_start=datetime.date(2026, 7, 1)):
    async with session_factory() as db:
        org = Organization(name="Acme", billing_cycle_day=cycle_day)
        db.add(org); await db.flush()
        db.add(BillingPeriod(org_id=org.id, start_date=period_start))
        await db.commit(); await db.refresh(org)
        return org


async def test_due_within_lead_window(session_factory, monkeypatch):
    _stub(monkeypatch, already_sent=False, lead=3)
    org = await _seed(session_factory)
    job = BillingReminderJob()
    async with session_factory() as db:
        # next boundary Aug 1; today Jul 30 -> 2 days out, within lead=3
        assert await job.is_due(db, org, datetime.date(2026, 7, 30)) is True
        # today Jul 20 -> 12 days out, outside lead
        assert await job.is_due(db, org, datetime.date(2026, 7, 20)) is False


async def test_not_due_when_already_sent(session_factory, monkeypatch):
    _stub(monkeypatch, already_sent=True, lead=3)
    org = await _seed(session_factory)
    job = BillingReminderJob()
    async with session_factory() as db:
        assert await job.is_due(db, org, datetime.date(2026, 7, 30)) is False


async def test_run_records_reminder_and_notifies(session_factory, monkeypatch):
    calls = {"reminder": 0, "notify": 0}
    _stub(monkeypatch, already_sent=False, lead=3, calls=calls)
    org = await _seed(session_factory)
    job = BillingReminderJob()
    async with session_factory() as db:
        res = await job.run(db, org, datetime.date(2026, 7, 30))
    assert res.outcome == "success"
    assert calls == {"reminder": 1, "notify": 1}


def _stub(monkeypatch, *, already_sent, lead, calls=None):
    calls = calls if calls is not None else {"reminder": 0, "notify": 0}
    async def _sent(db, org_id, period):
        return already_sent
    async def _lead(db, org_id):
        return lead
    async def _rec(**k):
        calls["reminder"] += 1; return 7
    async def _notify(*a, **k):
        calls["notify"] += 1; return 3
    monkeypatch.setattr("app.services.scheduler.jobs.billing_reminder.reminder_already_sent", _sent)
    monkeypatch.setattr("app.services.scheduler.jobs.billing_reminder.get_reminder_lead_days", _lead)
    monkeypatch.setattr("app.services.scheduler.jobs.billing_reminder.record_reminder", _rec)
    monkeypatch.setattr(
        "app.services.scheduler.jobs.billing_reminder.dispatch_notification_to_org_members", _notify
    )
