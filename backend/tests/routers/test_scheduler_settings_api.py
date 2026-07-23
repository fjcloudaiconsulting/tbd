"""Router tests for the per-org scheduler settings GET/PUT endpoint.

Mirrors the pattern in tests/routers/test_settings_feature_namespace.py:
a self-contained FastAPI app with dependency overrides for get_db and
get_current_user, backed by an in-memory SQLite session factory.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.deps import get_current_user, get_db
from app.models import Base
from app.models.user import Organization, Role, User
from app.routers import scheduler as scheduler_router
from app.security import hash_password


# ── fixtures ─────────────────────────────────────────────────────────────────


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


async def _seed(factory) -> dict:
    async with factory() as db:
        org = Organization(name="TestOrg", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        owner = User(
            org_id=org.id,
            username="owner",
            email="owner@test.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            is_active=True,
            email_verified=True,
        )
        member = User(
            org_id=org.id,
            username="member",
            email="member@test.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER,
            is_active=True,
            email_verified=True,
        )
        db.add_all([owner, member])
        await db.commit()
        return {"org_id": org.id, "owner_id": owner.id, "member_id": member.id}


async def _get_user(factory, user_id: int) -> User:
    async with factory() as db:
        return (await db.execute(select(User).where(User.id == user_id))).scalar_one()


def _make_app(session_factory, resolver):
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user() -> User:
        return await resolver(session_factory)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.include_router(scheduler_router.router)
    return app


# ── tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_returns_defaults(session_factory):
    ids = await _seed(session_factory)

    async def resolver(_f):
        return await _get_user(session_factory, ids["owner_id"])

    client = TestClient(_make_app(session_factory, resolver))
    r = client.get("/api/v1/scheduler/settings")
    assert r.status_code == 200
    assert r.json() == {
        "automate_recurring_generation": True,
        "automate_billing_close": True,
        "billing_close_reminder_lead_days": 3,
        "automate_cc_statement_alerts": True,
        "cc_statement_reminder_lead_days": 2,
    }


@pytest.mark.asyncio
async def test_put_updates_subset(session_factory):
    ids = await _seed(session_factory)

    async def resolver(_f):
        return await _get_user(session_factory, ids["owner_id"])

    client = TestClient(_make_app(session_factory, resolver))
    r = client.put(
        "/api/v1/scheduler/settings",
        json={"automate_billing_close": False, "billing_close_reminder_lead_days": 5},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["automate_billing_close"] is False
    assert body["billing_close_reminder_lead_days"] == 5
    assert body["automate_recurring_generation"] is True  # untouched


@pytest.mark.asyncio
async def test_put_rejects_out_of_range_lead_days(session_factory):
    ids = await _seed(session_factory)

    async def resolver(_f):
        return await _get_user(session_factory, ids["owner_id"])

    client = TestClient(_make_app(session_factory, resolver))
    r = client.put(
        "/api/v1/scheduler/settings",
        json={"billing_close_reminder_lead_days": 99},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_put_updates_cc_statement_fields(session_factory):
    ids = await _seed(session_factory)

    async def resolver(_f):
        return await _get_user(session_factory, ids["owner_id"])

    client = TestClient(_make_app(session_factory, resolver))
    r = client.put(
        "/api/v1/scheduler/settings",
        json={
            "automate_recurring_generation": True,
            "automate_billing_close": True,
            "billing_close_reminder_lead_days": 3,
            "automate_cc_statement_alerts": False,
            "cc_statement_reminder_lead_days": 5,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["automate_cc_statement_alerts"] is False
    assert body["cc_statement_reminder_lead_days"] == 5

    r2 = client.get("/api/v1/scheduler/settings")
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["automate_cc_statement_alerts"] is False
    assert body2["cc_statement_reminder_lead_days"] == 5


@pytest.mark.asyncio
async def test_put_rejects_out_of_range_cc_lead_days(session_factory):
    ids = await _seed(session_factory)

    async def resolver(_f):
        return await _get_user(session_factory, ids["owner_id"])

    client = TestClient(_make_app(session_factory, resolver))
    r = client.put(
        "/api/v1/scheduler/settings",
        json={"cc_statement_reminder_lead_days": 99},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_put_forbidden_for_non_admin(session_factory):
    ids = await _seed(session_factory)

    async def resolver(_f):
        return await _get_user(session_factory, ids["member_id"])

    client = TestClient(_make_app(session_factory, resolver))
    r = client.put(
        "/api/v1/scheduler/settings",
        json={"automate_billing_close": False},
    )
    assert r.status_code == 403
