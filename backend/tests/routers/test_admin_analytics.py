"""Router tests for L4.6 GET /api/v1/admin/analytics.

Pins:
- The auth gate (``analytics.view`` → superadmin short-circuits;
  non-superadmin gets 403).
- Response envelope shape (one round-trip).
- Window length echoed (``window_days``) and series are zero-filled
  to length ``window_days``.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user
from app.models import Base
from app.models.user import Organization, Role, User
from app.routers.admin_analytics import router as admin_analytics_router
from app.security import hash_password


@pytest_asyncio.fixture
async def session_factory():
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


def make_app(session_factory, current_user_resolver):
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user() -> User:
        return await current_user_resolver(session_factory)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.include_router(admin_analytics_router)
    return app


async def _seed_users(factory) -> dict:
    async with factory() as db:
        org = Organization(name="Platform", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        sa = User(
            org_id=org.id,
            username="root",
            email="root@platform.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            is_superadmin=True,
            is_active=True,
            email_verified=True,
        )
        plain = User(
            org_id=org.id,
            username="member",
            email="m@platform.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER,
            is_superadmin=False,
            is_active=True,
            email_verified=True,
        )
        db.add_all([sa, plain])
        await db.commit()
        return {"org_id": org.id, "sa_id": sa.id, "plain_id": plain.id}


def _superadmin_resolver():
    async def resolve(session_factory):
        from sqlalchemy import select as _select
        async with session_factory() as db:
            return (
                await db.execute(_select(User).where(User.is_superadmin.is_(True)))
            ).scalar_one()
    return resolve


def _plain_user_resolver():
    async def resolve(session_factory):
        from sqlalchemy import select as _select
        async with session_factory() as db:
            return (
                await db.execute(_select(User).where(User.is_superadmin.is_(False)))
            ).scalar_one()
    return resolve


@pytest.mark.asyncio
async def test_get_analytics_forbidden_for_non_superadmin(session_factory):
    await _seed_users(session_factory)
    app = make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/analytics")
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_get_analytics_returns_envelope_for_superadmin(session_factory):
    await _seed_users(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/analytics?days=14")
    assert res.status_code == 200
    body = res.json()
    assert body["window_days"] == 14
    assert "generated_at" in body
    # All three series are zero-filled to window length even when empty.
    assert len(body["logins_by_day"]) == 14
    assert len(body["tx_writes_by_day"]) == 14
    assert len(body["imports_by_day"]) == 14
    assert body["top_orgs_by_tx_volume"] == []
    # The Platform org has no transactions → it's dormant by default.
    dormant_names = {d["org_name"] for d in body["dormant_orgs"]}
    assert "Platform" in dormant_names


@pytest.mark.asyncio
async def test_get_analytics_rejects_out_of_range_days(session_factory):
    await _seed_users(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        # FastAPI Query(le=365) rejects above-range; route returns 422.
        res = client.get("/api/v1/admin/analytics?days=1000")
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_get_analytics_clamps_limit_params(session_factory):
    """top_orgs_limit and dormant_threshold_days are bounded; out-of-range
    values return 422 rather than silently clipping."""
    await _seed_users(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/analytics?top_orgs_limit=0")
    assert res.status_code == 422
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/analytics?dormant_threshold_days=-1")
    assert res.status_code == 422
