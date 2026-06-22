"""Unit tests for ``maybe_stamp_last_active`` (founding-members activity).

Pins the throttle: stamp when stale (``None`` or older than the window),
no-op when fresh, and a swallowed error never propagates.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.models import Base
from app.models.user import Organization, Role, User
from app.security import hash_password
from app.services.user_activity_service import maybe_stamp_last_active


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_user(factory, last_active_at: datetime | None) -> int:
    async with factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        user = User(
            org_id=org.id,
            username="alice",
            email="alice@acme.io",
            password_hash=hash_password("pw"),
            role=Role.OWNER,
            is_active=True,
            email_verified=True,
            last_active_at=last_active_at,
        )
        db.add(user)
        await db.commit()
        return user.id


async def _read_last_active(factory, user_id: int) -> datetime | None:
    async with factory() as db:
        return await db.scalar(
            select(User.last_active_at).where(User.id == user_id)
        )


@pytest.mark.asyncio
async def test_stamps_when_value_is_none(session_factory):
    uid = await _seed_user(session_factory, None)
    await maybe_stamp_last_active(session_factory, uid, None)
    assert await _read_last_active(session_factory, uid) is not None


@pytest.mark.asyncio
async def test_stamps_when_value_is_stale(session_factory):
    stale = datetime.now(timezone.utc) - timedelta(
        seconds=settings.last_active_stamp_throttle_seconds + 60
    )
    uid = await _seed_user(session_factory, stale)
    await maybe_stamp_last_active(session_factory, uid, stale)
    fresh = await _read_last_active(session_factory, uid)
    assert fresh is not None
    fresh_utc = fresh if fresh.tzinfo else fresh.replace(tzinfo=timezone.utc)
    assert fresh_utc > stale


@pytest.mark.asyncio
async def test_noop_when_value_is_fresh(session_factory):
    recent = datetime.now(timezone.utc) - timedelta(seconds=5)
    uid = await _seed_user(session_factory, recent)
    await maybe_stamp_last_active(session_factory, uid, recent)
    stored = await _read_last_active(session_factory, uid)
    stored_utc = stored if stored.tzinfo else stored.replace(tzinfo=timezone.utc)
    # Unchanged (within a second) — the throttle skipped the write.
    assert abs((stored_utc - recent).total_seconds()) < 1


@pytest.mark.asyncio
async def test_swallows_errors(session_factory):
    # A bogus session factory that raises on use must not propagate.
    class _Boom:
        def __call__(self):
            raise RuntimeError("db down")

    # Should not raise.
    await maybe_stamp_last_active(_Boom(), 1, None)
