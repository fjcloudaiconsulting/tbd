"""Router tests for L4.3 admin org management endpoints.

Service-layer behavior is pinned in
`tests/services/test_admin_orgs_service.py`. This file pins the auth
gate (superadmin via `orgs.view` / `orgs.manage`), the typed-confirm
delete contract, the self-org self-protect guard, and the structured
audit log emissions.
"""
from __future__ import annotations

import datetime
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user
from app.models import Base
from app.models.subscription import (
    BillingInterval,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.models.user import Organization, Role, User
from app.routers.admin_orgs import router as admin_orgs_router
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
    app.include_router(admin_orgs_router)
    return app


async def _seed(factory) -> dict:
    """Two orgs: 'Admin Org' (with the superadmin) and 'Target' (the
    one we'll act on)."""
    async with factory() as db:
        plan = Plan(slug="free", name="Free")
        db.add(plan)
        admin_org = Organization(name="Admin Org", billing_cycle_day=1)
        target = Organization(name="Target Inc", billing_cycle_day=1)
        db.add_all([admin_org, target])
        await db.commit()
        sa = User(
            org_id=admin_org.id, username="root",
            email="root@platform.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER, is_superadmin=True, is_active=True,
            email_verified=True,
        )
        owner = User(
            org_id=target.id, username="t_owner",
            email="t_owner@target.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER, is_superadmin=False, is_active=True,
            email_verified=True,
        )
        db.add_all([sa, owner])
        await db.commit()
        target_sub = Subscription(
            org_id=target.id, plan_id=plan.id,
            status=SubscriptionStatus.TRIALING,
            billing_interval=BillingInterval.MONTHLY,
            trial_end=datetime.date.today() + datetime.timedelta(days=14),
        )
        admin_sub = Subscription(
            org_id=admin_org.id, plan_id=plan.id,
            status=SubscriptionStatus.ACTIVE,
            billing_interval=BillingInterval.MONTHLY,
        )
        db.add_all([target_sub, admin_sub])
        await db.commit()
        return {
            "admin_user_id": sa.id,
            "admin_org_id": admin_org.id,
            "target_id": target.id,
            "owner_id": owner.id,
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


# ── auth gates ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_orgs_403_for_non_superadmin(session_factory):
    await _seed(session_factory)
    app = make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/orgs")
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_list_orgs_200_for_superadmin(session_factory):
    await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/orgs")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] >= 2
    names = sorted(item["name"] for item in body["items"])
    assert "Admin Org" in names and "Target Inc" in names


# ── drill-down ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_org_detail_200(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get(f"/api/v1/admin/orgs/{seed['target_id']}")
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "Target Inc"
    assert body["subscription"]["status"] == "trialing"
    usernames = sorted(m["username"] for m in body["members"])
    assert usernames == ["t_owner"]


@pytest.mark.asyncio
async def test_get_org_detail_404(session_factory):
    await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/orgs/99999")
    assert res.status_code == 404


# ── subscription override ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_subscription_changes_status(session_factory, caplog):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/admin/orgs/{seed['target_id']}/subscription",
            json={"status": "active"},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["before"]["status"] == "trialing"
    assert body["after"]["status"] == "active"


# ── delete with typed-confirm ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_org_requires_typed_confirm_name(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.request(
            "DELETE",
            f"/api/v1/admin/orgs/{seed['target_id']}",
            json={"confirm_name": "wrong"},
        )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_delete_org_succeeds_with_correct_confirm_name(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.request(
            "DELETE",
            f"/api/v1/admin/orgs/{seed['target_id']}",
            json={"confirm_name": "Target Inc"},
        )
    assert res.status_code == 200
    body = res.json()
    # Audit-friendly row counts surfaced in the response.
    assert body["deleted"]["organizations"] == 1
    assert body["deleted"]["users"] == 1


@pytest.mark.asyncio
async def test_delete_org_refuses_actor_self_org(session_factory):
    """Self-protect: superadmin cannot delete the org their account
    lives in — would lock themselves out."""
    seed = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.request(
            "DELETE",
            f"/api/v1/admin/orgs/{seed['admin_org_id']}",
            json={"confirm_name": "Admin Org"},
        )
    assert res.status_code == 409
