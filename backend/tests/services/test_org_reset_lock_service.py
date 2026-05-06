"""Tests for the per-org reset lock service.

Closes the residual risk logged on PR #134: two concurrent reset
POSTs could otherwise interleave through the per-batch commits and
the app-level idempotent seed and duplicate the post-wipe defaults.
"""
from __future__ import annotations

import datetime

import pytest
import pytest_asyncio
from sqlalchemy import event, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app._time import utcnow_naive
from app.models import Base
from app.models.org_data_reset_lock import OrgDataResetLock
from app.models.user import Organization, User
from app.services import org_reset_lock_service


@event.listens_for(Engine, "connect")
def _enable_sqlite_fk(conn, _record):  # noqa: D401
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_org_and_user(factory) -> tuple[int, int]:
    async with factory() as db:
        org = Organization(name="Acme")
        db.add(org)
        await db.flush()
        user = User(
            username="u",
            email="u@x.io",
            email_verified=True,
            password_hash="x",
            org_id=org.id,
            role="owner",
        )
        db.add(user)
        await db.commit()
        return org.id, user.id


@pytest.mark.asyncio
async def test_acquire_returns_token_when_no_existing_lock(session_factory):
    org_id, user_id = await _seed_org_and_user(session_factory)
    async with session_factory() as db:
        token = await org_reset_lock_service.acquire_reset_lock(
            db, org_id=org_id, user_id=user_id,
        )
    assert token is not None
    assert isinstance(token, str) and len(token) >= 32
    async with session_factory() as db:
        row = await db.scalar(
            select(OrgDataResetLock).where(OrgDataResetLock.org_id == org_id)
        )
        assert row is not None
        assert row.acquired_by_user_id == user_id
        assert row.lease_token == token


@pytest.mark.asyncio
async def test_acquire_returns_none_when_fresh_lock_already_held(session_factory):
    org_id, user_id = await _seed_org_and_user(session_factory)
    async with session_factory() as db:
        first = await org_reset_lock_service.acquire_reset_lock(
            db, org_id=org_id, user_id=user_id,
        )
    assert first is not None
    async with session_factory() as db:
        second = await org_reset_lock_service.acquire_reset_lock(
            db, org_id=org_id, user_id=user_id,
        )
    assert second is None


@pytest.mark.asyncio
async def test_acquire_overrides_stale_lock_with_new_token(session_factory):
    """A lock older than LOCK_TTL_MINUTES is overridable; the new
    acquire returns a *fresh* token, not the stale one.
    """
    org_id, user_id = await _seed_org_and_user(session_factory)
    stale_ts = utcnow_naive() - datetime.timedelta(hours=1)
    stale_token = "stale_token_aaaaaaaaaaaaaaaaaaaaaaaa"
    async with session_factory() as db:
        db.add(OrgDataResetLock(
            org_id=org_id,
            acquired_by_user_id=user_id,
            acquired_at=stale_ts,
            lease_token=stale_token,
        ))
        await db.commit()

    async with session_factory() as db:
        new_token = await org_reset_lock_service.acquire_reset_lock(
            db, org_id=org_id, user_id=user_id,
        )
    assert new_token is not None
    assert new_token != stale_token

    async with session_factory() as db:
        row = await db.scalar(
            select(OrgDataResetLock).where(OrgDataResetLock.org_id == org_id)
        )
        assert row.acquired_at > stale_ts
        assert row.lease_token == new_token


@pytest.mark.asyncio
async def test_release_with_correct_token_clears_the_lock(session_factory):
    org_id, user_id = await _seed_org_and_user(session_factory)
    async with session_factory() as db:
        token = await org_reset_lock_service.acquire_reset_lock(
            db, org_id=org_id, user_id=user_id,
        )
    assert token is not None

    async with session_factory() as db:
        await org_reset_lock_service.release_reset_lock(db, org_id=org_id, token=token)

    async with session_factory() as db:
        row = await db.scalar(
            select(OrgDataResetLock).where(OrgDataResetLock.org_id == org_id)
        )
    assert row is None


@pytest.mark.asyncio
async def test_release_with_stale_token_does_not_delete_successor_lock(session_factory):
    """Critical regression: the original review finding.

    Reset A acquires (token T_A), stalls past TTL, reset B takes the
    lock over (token T_B), then A wakes up and calls release with
    its own (now-stale) token. A must NOT delete B's fresh lock,
    or reset C could start while B is still running and reopen the
    interleave window the lock is meant to close.
    """
    org_id, user_id = await _seed_org_and_user(session_factory)

    # A acquires.
    async with session_factory() as db:
        token_a = await org_reset_lock_service.acquire_reset_lock(
            db, org_id=org_id, user_id=user_id,
        )
    assert token_a is not None

    # A stalls. Simulate by manually aging the row past LOCK_TTL_MINUTES.
    stale_ts = utcnow_naive() - datetime.timedelta(hours=1)
    async with session_factory() as db:
        await db.execute(
            update(OrgDataResetLock)
            .where(OrgDataResetLock.org_id == org_id)
            .values(acquired_at=stale_ts)
        )
        await db.commit()

    # B takes the lock over via stale-takeover.
    async with session_factory() as db:
        token_b = await org_reset_lock_service.acquire_reset_lock(
            db, org_id=org_id, user_id=user_id,
        )
    assert token_b is not None
    assert token_b != token_a

    # A finally wakes and tries to release with its OLD token.
    async with session_factory() as db:
        await org_reset_lock_service.release_reset_lock(
            db, org_id=org_id, token=token_a,
        )

    # B's lock must still be present.
    async with session_factory() as db:
        row = await db.scalar(
            select(OrgDataResetLock).where(OrgDataResetLock.org_id == org_id)
        )
    assert row is not None, (
        "stale-token release must not delete the successor's lock"
    )
    assert row.lease_token == token_b

    # And `is_reset_locked` still reports busy — reset C cannot start.
    async with session_factory() as db:
        assert await org_reset_lock_service.is_reset_locked(
            db, org_id=org_id
        ) is True


@pytest.mark.asyncio
async def test_release_idempotent_on_correct_token(session_factory):
    """Calling release twice with the correct token (or with no row
    present) is a no-op the second time.
    """
    org_id, user_id = await _seed_org_and_user(session_factory)

    async with session_factory() as db:
        token = await org_reset_lock_service.acquire_reset_lock(
            db, org_id=org_id, user_id=user_id,
        )
    assert token is not None

    async with session_factory() as db:
        await org_reset_lock_service.release_reset_lock(db, org_id=org_id, token=token)
    async with session_factory() as db:
        await org_reset_lock_service.release_reset_lock(db, org_id=org_id, token=token)

    async with session_factory() as db:
        row = await db.scalar(
            select(OrgDataResetLock).where(OrgDataResetLock.org_id == org_id)
        )
    assert row is None


@pytest.mark.asyncio
async def test_acquire_release_acquire_cycle(session_factory):
    """After a release, the next acquire succeeds again with a new token."""
    org_id, user_id = await _seed_org_and_user(session_factory)

    async with session_factory() as db:
        first = await org_reset_lock_service.acquire_reset_lock(
            db, org_id=org_id, user_id=user_id,
        )
    assert first is not None

    async with session_factory() as db:
        await org_reset_lock_service.release_reset_lock(db, org_id=org_id, token=first)

    async with session_factory() as db:
        second = await org_reset_lock_service.acquire_reset_lock(
            db, org_id=org_id, user_id=user_id,
        )
    assert second is not None
    assert second != first


@pytest.mark.asyncio
async def test_is_reset_locked_reflects_state(session_factory):
    org_id, user_id = await _seed_org_and_user(session_factory)

    async with session_factory() as db:
        assert await org_reset_lock_service.is_reset_locked(db, org_id=org_id) is False

    async with session_factory() as db:
        token = await org_reset_lock_service.acquire_reset_lock(
            db, org_id=org_id, user_id=user_id,
        )
    async with session_factory() as db:
        assert await org_reset_lock_service.is_reset_locked(db, org_id=org_id) is True

    async with session_factory() as db:
        await org_reset_lock_service.release_reset_lock(db, org_id=org_id, token=token)
    async with session_factory() as db:
        assert await org_reset_lock_service.is_reset_locked(db, org_id=org_id) is False
