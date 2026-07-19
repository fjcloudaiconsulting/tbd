"""Tests for the EmailBroadcast / EmailBroadcastRecipient models.

Covers:
- BroadcastStatus defaults to DRAFT and the three counter columns default to 0.
- UNIQUE(broadcast_id, user_id) on email_broadcast_recipients rejects a
  duplicate materialization row.

Uses an in-memory aiosqlite engine (project convention, same pattern as
test_dashboard_layout.py) so no running MySQL / docker-compose stack is
required. The real MySQL enum-DDL check is a separate manual merge gate
(spec Ruling 5), not this unit test.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.email_broadcast import (
    BroadcastStatus,
    RecipientStatus,
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


@pytest.mark.asyncio
async def test_broadcast_defaults_and_recipient_unique(session_factory):
    async with session_factory() as db:
        # Recipient.user_id is a real FK to users.id and the fixture turns on
        # SQLite FK enforcement, so a real Organization + User must exist
        # before a recipient row can reference it.
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
        await db.commit()
        await db.refresh(b)
        assert b.status == BroadcastStatus.DRAFT
        assert b.sent_count == 0 and b.failed_count == 0 and b.skipped_count == 0

        db.add(
            EmailBroadcastRecipient(
                broadcast_id=b.id,
                user_id=user.id,
                email="a@x.io",
                status=RecipientStatus.PENDING,
            )
        )
        await db.commit()

        db.add(EmailBroadcastRecipient(broadcast_id=b.id, user_id=user.id, email="a@x.io"))
        with pytest.raises(IntegrityError):
            await db.commit()
