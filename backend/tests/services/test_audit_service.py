"""Service-layer tests for L4.7 audit_service.

Two properties to pin tightly:

1. ``record_audit_event`` opens its OWN session through the factory
   and commits — so an audit row exists even when the caller's session
   was rolled back.
2. ``record_audit_event`` NEVER raises. A broken factory must be
   absorbed; the caller's structlog event is the fallback channel.
"""
from __future__ import annotations

import datetime
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.audit_event import AuditEvent, AuditOutcome
from app.services.audit_service import list_audit_events, record_audit_event


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(Engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


# ── recording ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_audit_event_commits_independently(session_factory):
    """Audit row is visible to a freshly-opened session — i.e. it was
    actually committed, not just flushed inside the recorder's
    not-yet-committed scope.
    """
    await record_audit_event(
        session_factory,
        event_type="admin.org.delete",
        actor_user_id=None,
        actor_email="root@example.io",
        target_org_id=None,
        target_org_name="Some Org",
        request_id="abc123",
        ip_address="10.0.0.1",
        outcome="success",
        detail={"k": "v"},
    )

    async with session_factory() as db:
        rows = (await db.execute(select(AuditEvent))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.event_type == "admin.org.delete"
    assert row.actor_email == "root@example.io"
    assert row.target_org_name == "Some Org"
    assert row.request_id == "abc123"
    assert row.ip_address == "10.0.0.1"
    assert row.outcome == AuditOutcome.SUCCESS
    assert row.detail == {"k": "v"}


@pytest.mark.asyncio
async def test_record_audit_event_survives_bad_session():
    """If the factory itself blows up, record_audit_event must NOT
    raise — the structlog event the caller already emitted is the
    backup channel and we don't want to mask the original 200/500 the
    user sees.
    """
    def broken_factory():
        raise RuntimeError("DB unreachable")

    # Should not raise.
    await record_audit_event(
        broken_factory,
        event_type="admin.org.delete",
        actor_user_id=None,
        actor_email="root@example.io",
        target_org_id=None,
        target_org_name=None,
        request_id=None,
        ip_address=None,
        outcome="failure",
        detail=None,
    )


# ── querying ────────────────────────────────────────────────────────────


async def _seed_three(factory) -> None:
    """Three rows: 2 success, 1 failure, spread across two timestamps."""
    base = datetime.datetime(2026, 5, 1, 9, 0, 0)
    async with factory() as db:
        db.add_all([
            AuditEvent(
                event_type="admin.org.delete",
                actor_user_id=None,
                actor_email="a@x.io",
                target_org_id=None,
                target_org_name="A",
                request_id="r1",
                ip_address=None,
                outcome=AuditOutcome.SUCCESS,
                detail=None,
                created_at=base,
            ),
            AuditEvent(
                event_type="admin.org.delete.failed",
                actor_user_id=None,
                actor_email="b@x.io",
                target_org_id=None,
                target_org_name="B",
                request_id="r2",
                ip_address=None,
                outcome=AuditOutcome.FAILURE,
                detail=None,
                created_at=base + datetime.timedelta(hours=1),
            ),
            AuditEvent(
                event_type="admin.org.subscription.override",
                actor_user_id=None,
                actor_email="c@x.io",
                target_org_id=None,
                target_org_name="C",
                request_id="r3",
                ip_address=None,
                outcome=AuditOutcome.SUCCESS,
                detail=None,
                created_at=base + datetime.timedelta(hours=2),
            ),
        ])
        await db.commit()


@pytest.mark.asyncio
async def test_list_audit_events_filters_by_outcome(session_factory):
    await _seed_three(session_factory)
    async with session_factory() as db:
        rows, total = await list_audit_events(db, outcome="failure")
    assert total == 1
    assert len(rows) == 1
    assert rows[0].event_type == "admin.org.delete.failed"


@pytest.mark.asyncio
async def test_list_audit_events_date_range(session_factory):
    await _seed_three(session_factory)
    async with session_factory() as db:
        rows, total = await list_audit_events(
            db,
            from_dt=datetime.datetime(2026, 5, 1, 9, 30, 0),
            to_dt=datetime.datetime(2026, 5, 1, 10, 30, 0),
        )
    # The middle event (10:00) is the only one inside the window.
    assert total == 1
    assert rows[0].event_type == "admin.org.delete.failed"


@pytest.mark.asyncio
async def test_list_audit_events_orders_newest_first(session_factory):
    await _seed_three(session_factory)
    async with session_factory() as db:
        rows, total = await list_audit_events(db)
    assert total == 3
    # Newest first — the 11:00 row leads.
    assert rows[0].event_type == "admin.org.subscription.override"
    assert rows[-1].event_type == "admin.org.delete"


@pytest.mark.asyncio
async def test_list_audit_events_pagination(session_factory):
    await _seed_three(session_factory)
    async with session_factory() as db:
        rows, total = await list_audit_events(db, limit=2, offset=0)
    assert total == 3
    assert len(rows) == 2
    async with session_factory() as db:
        rows2, total2 = await list_audit_events(db, limit=2, offset=2)
    assert total2 == 3
    assert len(rows2) == 1
