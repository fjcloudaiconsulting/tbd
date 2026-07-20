"""Tests for the delivery_status / delivery_updated_at columns added to
EmailBroadcastRecipient (Mailgun delivery webhooks, spec 2026-07-20, Task 1).

Covers:
- Both columns default to None on a freshly-created recipient row.
- A recipient row round-trips delivery_status="delivered" plus a
  delivery_updated_at timestamp.

Uses an in-memory aiosqlite engine (project convention, same pattern as
test_email_broadcast_model.py). The real MySQL migration verification
(nullable ADD COLUMN + index) is a separate manual merge gate (Ruling W8),
not this unit test.
"""
from __future__ import annotations

from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.email_broadcast import (
    SEGMENT_ACTIVE_VERIFIED,
    EmailBroadcast,
    EmailBroadcastRecipient,
)
from app.models.user import Organization, Role, User


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(Engine, "connect")
    def _fk_on(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _seed_broadcast_and_user(db):
    org = Organization(name="TestOrg", billing_cycle_day=1)
    db.add(org)
    await db.flush()
    user = User(
        username="user1",
        email="a@x.io",
        password_hash="hashed",
        org_id=org.id,
        role=Role.OWNER,
    )
    db.add(user)
    await db.flush()

    b = EmailBroadcast(
        subject="Hi",
        body_template="Hi {first_name},",
        segment=SEGMENT_ACTIVE_VERIFIED,
    )
    db.add(b)
    await db.flush()
    return b, user


@pytest.mark.asyncio
async def test_delivery_columns_default_none(session_factory):
    async with session_factory() as db:
        b, user = await _seed_broadcast_and_user(db)
        recipient = EmailBroadcastRecipient(
            broadcast_id=b.id,
            user_id=user.id,
            email="a@x.io",
        )
        db.add(recipient)
        await db.commit()
        await db.refresh(recipient)

        assert recipient.delivery_status is None
        assert recipient.delivery_updated_at is None


@pytest.mark.asyncio
async def test_delivery_columns_round_trip(session_factory):
    async with session_factory() as db:
        b, user = await _seed_broadcast_and_user(db)
        now = datetime(2026, 7, 20, 12, 0, 0)
        recipient = EmailBroadcastRecipient(
            broadcast_id=b.id,
            user_id=user.id,
            email="a@x.io",
            delivery_status="delivered",
            delivery_updated_at=now,
        )
        db.add(recipient)
        await db.commit()
        await db.refresh(recipient)

        assert recipient.delivery_status == "delivered"
        assert recipient.delivery_updated_at == now
