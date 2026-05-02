"""Router tests for L4.11 plan duplicate endpoint.

`POST /api/v1/plans/{plan_id}/duplicate` clones an existing plan with
`is_custom=True` and re-canonicalizes features so the clone always
has the full closed-set feature keys. plans.manage gates the
endpoint (superadmin short-circuits in the current permission scheme).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

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
from app.models.subscription import Plan
from app.models.user import Organization, Role, User
from app.routers.plans import router as plans_router
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
    app.include_router(plans_router)
    return app


async def _seed(factory) -> dict:
    """One org + superadmin + plain user, plus a 'pro' source plan
    with ai.budget=True and an 'enterprise' plan we can collide
    against on slug."""
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
            org_id=org.id, username="plain",
            email="plain@platform.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER, is_superadmin=False, is_active=True,
            email_verified=True,
        )
        db.add_all([sa, plain])

        pro = Plan(
            name="Pro",
            slug="pro",
            description="Pro plan",
            is_custom=False,
            is_active=True,
            sort_order=10,
            price_monthly=Decimal("19.99"),
            price_yearly=Decimal("199.00"),
            max_users=10,
            retention_days=365,
            features={"ai.budget": True, "ai.forecast": False, "ai.smart_plan": False},
        )
        enterprise = Plan(
            name="Enterprise",
            slug="enterprise",
            description="Enterprise plan",
            is_custom=False,
            is_active=True,
            sort_order=20,
            price_monthly=Decimal("99.00"),
            price_yearly=Decimal("990.00"),
            max_users=None,
            retention_days=None,
            features={"ai.budget": True, "ai.forecast": True, "ai.smart_plan": True},
        )
        db.add_all([pro, enterprise])
        await db.commit()

        return {
            "admin_user_id": sa.id,
            "plain_user_id": plain.id,
            "pro_id": pro.id,
            "enterprise_id": enterprise.id,
        }


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


# ── happy path ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_clones_with_is_custom_true(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/plans/{seed['pro_id']}/duplicate",
            json={"name": "Pro - ACME", "slug": "pro-acme"},
        )
    assert res.status_code == 201
    body = res.json()
    assert body["name"] == "Pro - ACME"
    assert body["slug"] == "pro-acme"
    assert body["is_custom"] is True
    assert body["is_active"] is True
    # features carry through (and are canonicalized)
    assert body["features"]["ai.budget"] is True
    assert body["features"]["ai.forecast"] is False
    assert body["features"]["ai.smart_plan"] is False


# ── 409 on slug conflict ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_409_on_slug_conflict(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/plans/{seed['pro_id']}/duplicate",
            json={"name": "Whatever", "slug": "enterprise"},
        )
    assert res.status_code == 409


# ── 404 on missing source ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_404_on_missing_source(session_factory):
    await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/plans/999999/duplicate",
            json={"name": "Ghost", "slug": "ghost"},
        )
    assert res.status_code == 404


# ── auth gate ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_requires_plans_manage(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/plans/{seed['pro_id']}/duplicate",
            json={"name": "Pro - ACME", "slug": "pro-acme"},
        )
    assert res.status_code == 403
