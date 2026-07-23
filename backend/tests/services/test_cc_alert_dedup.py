from __future__ import annotations

import datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
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
    # record_cc_alert opens its own session via app.database.async_session
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


async def test_record_then_sent_set_contains_pair(session_factory, org):
    cd = datetime.date(2026, 7, 20)
    await sched_audit.record_cc_alert(
        org=org,
        account_id=7,
        close_date=cd,
        event_type=sched_audit.CC_CLOSED_EVENT_TYPE,
        detail={},
    )
    async with session_factory() as db:
        sent = await sched_audit.cc_alerts_sent_since(
            db, org.id, sched_audit.CC_CLOSED_EVENT_TYPE, datetime.date(2026, 7, 1)
        )
    assert (7, "2026-07-20") in sent


async def test_window_excludes_old(session_factory, org):
    cd = datetime.date(2026, 7, 20)
    await sched_audit.record_cc_alert(
        org=org,
        account_id=7,
        close_date=cd,
        event_type=sched_audit.CC_CLOSED_EVENT_TYPE,
        detail={},
    )
    async with session_factory() as db:
        sent = await sched_audit.cc_alerts_sent_since(
            db, org.id, sched_audit.CC_CLOSED_EVENT_TYPE, datetime.date(2026, 7, 25)
        )
    assert (7, "2026-07-20") not in sent


async def test_no_amount_stored_in_detail(session_factory, org):
    """Security constraint: dollar amounts must never land in the audit detail."""
    cd = datetime.date(2026, 7, 20)
    await sched_audit.record_cc_alert(
        org=org,
        account_id=9,
        close_date=cd,
        event_type=sched_audit.CC_REMINDER_EVENT_TYPE,
        detail={"foo": "bar"},
    )
    async with session_factory() as db:
        from sqlalchemy import select

        from app.models.audit_event import AuditEvent

        rows = (await db.execute(select(AuditEvent))).scalars().all()
    assert len(rows) == 1
    detail = rows[0].detail
    assert detail["account_id"] == 9
    assert detail["close_date"] == "2026-07-20"
    assert "amount" not in detail
    assert not any("amount" in k.lower() for k in detail.keys())


async def test_event_types_are_distinct():
    assert sched_audit.CC_REMINDER_EVENT_TYPE == "scheduler.cc_statement.reminder"
    assert sched_audit.CC_CLOSED_EVENT_TYPE == "scheduler.cc_statement.closed"
    assert sched_audit.CC_REMINDER_EVENT_TYPE != sched_audit.CC_CLOSED_EVENT_TYPE
