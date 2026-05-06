"""Router tests for L4.7 GET /api/v1/admin/audit.

Pins:
- The auth gate (audit.view → superadmin short-circuit; non-superadmin
  gets 403).
- Response shape and ordering (newest first).
- Pagination (limit/offset surface total + items correctly).
"""
from __future__ import annotations

import datetime
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user
from app.models import Base
from app.models.audit_event import AuditEvent, AuditOutcome
from app.models.user import Organization, Role, User
from app.routers.admin_audit import router as admin_audit_router
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
    app.include_router(admin_audit_router)
    return app


async def _seed(factory) -> dict:
    async with factory() as db:
        org = Organization(name="Audit Org", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        sa = User(
            org_id=org.id, username="root",
            email="root@platform.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER, is_superadmin=True, is_active=True,
            email_verified=True,
        )
        plain = User(
            org_id=org.id, username="user",
            email="u@platform.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER, is_superadmin=False, is_active=True,
            email_verified=True,
        )
        db.add_all([sa, plain])
        await db.commit()
        return {"org_id": org.id, "sa_id": sa.id, "plain_id": plain.id}


async def _seed_events(factory, n: int) -> None:
    base = datetime.datetime(2026, 5, 1, 9, 0, 0)
    async with factory() as db:
        for i in range(n):
            db.add(
                AuditEvent(
                    event_type=f"admin.org.event.{i}",
                    actor_user_id=None,
                    actor_email=f"actor-{i}@x.io",
                    target_org_id=None,
                    target_org_name=f"Org-{i}",
                    request_id=f"req-{i}",
                    ip_address="10.0.0.1",
                    outcome=AuditOutcome.SUCCESS,
                    detail={"i": i},
                    created_at=base + datetime.timedelta(minutes=i),
                )
            )
        await db.commit()


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
async def test_get_audit_list_requires_superadmin(session_factory):
    await _seed(session_factory)
    await _seed_events(session_factory, 1)
    app = make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/audit")
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_get_audit_list_returns_events(session_factory):
    await _seed(session_factory)
    await _seed_events(session_factory, 3)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/audit")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 3
    assert len(body["items"]) == 3
    # Newest first — index 2 is the last seeded (latest timestamp).
    assert body["items"][0]["event_type"] == "admin.org.event.2"
    assert body["items"][0]["outcome"] == "success"
    assert body["items"][0]["request_id"] == "req-2"
    assert body["items"][0]["target_org_name"] == "Org-2"
    assert body["items"][0]["detail"] == {"i": 2}


@pytest.mark.asyncio
async def test_get_audit_list_pagination(session_factory):
    await _seed(session_factory)
    await _seed_events(session_factory, 5)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/audit?limit=2&offset=0")
        assert res.status_code == 200
        body = res.json()
        assert body["total"] == 5
        assert len(body["items"]) == 2

        res2 = client.get("/api/v1/admin/audit?limit=2&offset=4")
        body2 = res2.json()
        assert body2["total"] == 5
        assert len(body2["items"]) == 1
