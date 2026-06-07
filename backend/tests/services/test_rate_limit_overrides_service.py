"""Tests for ``rate_limit_overrides_service`` (L4.10).

Covers the architect-locked invariants:

- exactly-one-of (org_id, user_id) on create.
- resolve order user > org > none.
- ``expires_at`` in the past treated as absent.
- cache miss / hit / negative-cache plumbing (Redis stubbed).
- update path invalidates cache for both old and new endpoint
  patterns when the pattern moves.
- delete invalidates cache.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.rate_limit_override import RateLimitOverride
from app.models.user import Organization, User
from app.services import rate_limit_overrides_service as svc


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


class _FakeRedis:
    """Minimal Redis stub. Implements ``get``, ``set``, ``delete`` with
    no TTL handling (the service never reads TTL back). All operations
    are sync to mirror redis-py's sync API surface.
    """

    def __init__(self):
        self.store: dict[str, str] = {}
        self.calls: list[tuple[str, ...]] = []

    def get(self, key):
        self.calls.append(("get", key))
        v = self.store.get(key)
        # redis-py returns bytes by default.
        return v.encode("utf-8") if isinstance(v, str) else v

    def set(self, key, value, ex=None):
        self.calls.append(("set", key, value, ex))
        self.store[key] = value

    def delete(self, key):
        self.calls.append(("delete", key))
        self.store.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch) -> _FakeRedis:
    r = _FakeRedis()

    def _get_client():
        return r

    # Patch where the service imports it (the lazy import path inside
    # the service helpers).
    import app.redis_client as rc

    monkeypatch.setattr(rc, "get_client", _get_client)
    return r


async def _seed_org_user(factory) -> tuple[int, int]:
    async with factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        await db.refresh(org)
        u = User(
            org_id=org.id,
            username="u",
            email="u@example.com",
            password_hash="x",
            role="owner",
        )
        db.add(u)
        await db.commit()
        await db.refresh(u)
        return org.id, u.id


@pytest.mark.asyncio
async def test_create_requires_exactly_one_scope(session_factory, fake_redis):
    org_id, user_id = await _seed_org_user(session_factory)
    async with session_factory() as db:
        with pytest.raises(ValueError):
            await svc.create_override(
                db,
                org_id=org_id,
                user_id=user_id,
                endpoint_pattern="auth.login",
                max_requests=10,
                period_seconds=60,
                expires_at=None,
                created_by_user_id=None,
                note=None,
            )
        with pytest.raises(ValueError):
            await svc.create_override(
                db,
                org_id=None,
                user_id=None,
                endpoint_pattern="auth.login",
                max_requests=10,
                period_seconds=60,
                expires_at=None,
                created_by_user_id=None,
                note=None,
            )


@pytest.mark.asyncio
async def test_resolve_returns_none_when_no_override(session_factory, fake_redis):
    org_id, user_id = await _seed_org_user(session_factory)
    async with session_factory() as db:
        result = await svc.resolve_override(
            db,
            user_id=user_id,
            org_id=org_id,
            endpoint_pattern="auth.login",
        )
    assert result is None
    # Both scopes should have written negative-cache rows so a second
    # lookup short-circuits in Redis.
    assert "rate_limit_override:user:" in "\n".join(
        " ".join(str(c) for c in call) for call in fake_redis.calls
    )


@pytest.mark.asyncio
async def test_resolve_user_wins_over_org(session_factory, fake_redis):
    org_id, user_id = await _seed_org_user(session_factory)
    async with session_factory() as db:
        await svc.create_override(
            db,
            org_id=org_id,
            user_id=None,
            endpoint_pattern="auth.login",
            max_requests=100,
            period_seconds=60,
            expires_at=None,
            created_by_user_id=None,
            note=None,
        )
        await svc.create_override(
            db,
            org_id=None,
            user_id=user_id,
            endpoint_pattern="auth.login",
            max_requests=5,
            period_seconds=60,
            expires_at=None,
            created_by_user_id=None,
            note=None,
        )
    # Fresh session for the resolver to clear ORM identity cache.
    async with session_factory() as db:
        result = await svc.resolve_override(
            db,
            user_id=user_id,
            org_id=org_id,
            endpoint_pattern="auth.login",
        )
    # User override wins.
    assert result == "5/60"


@pytest.mark.asyncio
async def test_resolve_falls_through_to_org(session_factory, fake_redis):
    org_id, user_id = await _seed_org_user(session_factory)
    async with session_factory() as db:
        await svc.create_override(
            db,
            org_id=org_id,
            user_id=None,
            endpoint_pattern="auth.login",
            max_requests=200,
            period_seconds=60,
            expires_at=None,
            created_by_user_id=None,
            note=None,
        )
    async with session_factory() as db:
        result = await svc.resolve_override(
            db,
            user_id=user_id,
            org_id=org_id,
            endpoint_pattern="auth.login",
        )
    assert result == "200/60"


@pytest.mark.asyncio
async def test_resolve_ignores_expired_row(session_factory, fake_redis):
    org_id, _ = await _seed_org_user(session_factory)
    past = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
    async with session_factory() as db:
        await svc.create_override(
            db,
            org_id=org_id,
            user_id=None,
            endpoint_pattern="auth.login",
            max_requests=200,
            period_seconds=60,
            expires_at=past,
            created_by_user_id=None,
            note=None,
        )
    async with session_factory() as db:
        result = await svc.resolve_override(
            db,
            user_id=None,
            org_id=org_id,
            endpoint_pattern="auth.login",
        )
    assert result is None


@pytest.mark.asyncio
async def test_resolve_honours_future_expiry(session_factory, fake_redis):
    org_id, _ = await _seed_org_user(session_factory)
    future = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
    async with session_factory() as db:
        await svc.create_override(
            db,
            org_id=org_id,
            user_id=None,
            endpoint_pattern="auth.login",
            max_requests=200,
            period_seconds=60,
            expires_at=future,
            created_by_user_id=None,
            note=None,
        )
    async with session_factory() as db:
        result = await svc.resolve_override(
            db,
            user_id=None,
            org_id=org_id,
            endpoint_pattern="auth.login",
        )
    assert result == "200/60"


@pytest.mark.asyncio
async def test_cache_hit_skips_db(session_factory, fake_redis):
    """If Redis has a positive cache, the resolver returns it without
    issuing a DB read. We assert this by seeding Redis directly and
    not creating a DB row.
    """
    org_id, _ = await _seed_org_user(session_factory)
    fake_redis.store[
        f"rate_limit_override:org:{org_id}:auth.login"
    ] = "42/60"
    async with session_factory() as db:
        result = await svc.resolve_override(
            db,
            user_id=None,
            org_id=org_id,
            endpoint_pattern="auth.login",
        )
    assert result == "42/60"


@pytest.mark.asyncio
async def test_update_moves_cache_on_pattern_change(session_factory, fake_redis):
    org_id, _ = await _seed_org_user(session_factory)
    async with session_factory() as db:
        row = await svc.create_override(
            db,
            org_id=org_id,
            user_id=None,
            endpoint_pattern="auth.login",
            max_requests=10,
            period_seconds=60,
            expires_at=None,
            created_by_user_id=None,
            note=None,
        )
        # Pre-seed Redis to assert the old key gets invalidated.
        fake_redis.store[
            f"rate_limit_override:org:{org_id}:auth.login"
        ] = "10/60"
        await svc.update_override(
            db,
            row=row,
            patch={"endpoint_pattern": "auth.register"},
        )
    delete_keys = [
        c[1] for c in fake_redis.calls if c[0] == "delete"
    ]
    assert f"rate_limit_override:org:{org_id}:auth.login" in delete_keys
    assert f"rate_limit_override:org:{org_id}:auth.register" in delete_keys


@pytest.mark.asyncio
async def test_delete_invalidates_cache(session_factory, fake_redis):
    org_id, _ = await _seed_org_user(session_factory)
    async with session_factory() as db:
        row = await svc.create_override(
            db,
            org_id=org_id,
            user_id=None,
            endpoint_pattern="auth.login",
            max_requests=10,
            period_seconds=60,
            expires_at=None,
            created_by_user_id=None,
            note=None,
        )
        fake_redis.store[
            f"rate_limit_override:org:{org_id}:auth.login"
        ] = "10/60"
        await svc.delete_override(db, row=row)
    delete_keys = [
        c[1] for c in fake_redis.calls if c[0] == "delete"
    ]
    assert f"rate_limit_override:org:{org_id}:auth.login" in delete_keys


@pytest.mark.asyncio
async def test_list_filters_by_scope(session_factory, fake_redis):
    org_id, user_id = await _seed_org_user(session_factory)
    async with session_factory() as db:
        await svc.create_override(
            db,
            org_id=org_id,
            user_id=None,
            endpoint_pattern="auth.login",
            max_requests=10,
            period_seconds=60,
            expires_at=None,
            created_by_user_id=None,
            note=None,
        )
        await svc.create_override(
            db,
            org_id=None,
            user_id=user_id,
            endpoint_pattern="auth.login",
            max_requests=5,
            period_seconds=60,
            expires_at=None,
            created_by_user_id=None,
            note=None,
        )
    async with session_factory() as db:
        org_rows, org_total = await svc.list_overrides(db, org_id=org_id)
        user_rows, user_total = await svc.list_overrides(db, user_id=user_id)
        all_rows, all_total = await svc.list_overrides(db)
    assert org_total == 1
    assert user_total == 1
    assert all_total == 2
    assert org_rows[0].org_id == org_id
    assert user_rows[0].user_id == user_id


@pytest.mark.asyncio
async def test_cache_negative_then_positive(session_factory, fake_redis):
    """A negative cache row for org should NOT short-circuit a
    subsequent user-scope override read on the same endpoint.
    """
    org_id, user_id = await _seed_org_user(session_factory)
    # Pre-cache a negative for user; org will fall through to DB.
    fake_redis.store[
        f"rate_limit_override:user:{user_id}:auth.login"
    ] = "-"
    async with session_factory() as db:
        await svc.create_override(
            db,
            org_id=org_id,
            user_id=None,
            endpoint_pattern="auth.login",
            max_requests=200,
            period_seconds=60,
            expires_at=None,
            created_by_user_id=None,
            note=None,
        )
    async with session_factory() as db:
        result = await svc.resolve_override(
            db,
            user_id=user_id,
            org_id=org_id,
            endpoint_pattern="auth.login",
        )
    # User cache says no -> falls through to org -> hits DB -> "200/60".
    assert result == "200/60"
