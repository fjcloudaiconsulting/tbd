"""Router tests for L4.11 admin feature-catalog endpoint.

`GET /api/v1/admin/feature-catalog` exposes the canonical set of
feature keys for the admin UI's plan-features editor. plans.manage
gates it — anyone who can edit plan features needs to know which keys
exist. This file pins the auth gate and the deterministic sort order.
"""
from __future__ import annotations

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
from app.models.user import Organization, Role, User
from app.routers.admin import router as admin_router
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
    app.include_router(admin_router)
    return app


async def _seed(factory) -> dict:
    """One org with a superadmin and a regular user."""
    async with factory() as db:
        org = Organization(name="Admin Org", billing_cycle_day=1)
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
            email="user@platform.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER, is_superadmin=False, is_active=True,
            email_verified=True,
        )
        db.add_all([sa, plain])
        await db.commit()
        return {"admin_user_id": sa.id, "plain_user_id": plain.id}


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
async def test_catalog_returns_sorted_keys(session_factory):
    await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/feature-catalog")
    assert res.status_code == 200
    assert res.json() == {
        "keys": [
            "ai.autocategorize",
            "ai.budget",
            "ai.forecast",
            "ai.smart_plan",
        ]
    }


@pytest.mark.asyncio
async def test_catalog_requires_plans_manage(session_factory):
    await _seed(session_factory)
    app = make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/feature-catalog")
    assert res.status_code == 403
