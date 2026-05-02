"""Router tests for L4.11 admin feature-state composite endpoint (T16).

`GET /api/v1/admin/orgs/{org_id}/feature-state` composes plan
defaults with active org overrides into a per-key list ordered by
`sorted(ALL_FEATURE_KEYS)`. Each row carries `plan_default`,
`effective`, and the (optional) override block with the setter's
email resolved server-side.

Auth gate is `orgs.view` — superadmin short-circuits in the current
permission scheme; non-superadmin gets 403.
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

from app.auth.feature_catalog import ALL_FEATURE_KEYS
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


async def _seed(factory, *, plan_slug: str, plan_name: str, plan_features: dict) -> dict:
    """Two orgs, one plan, one subscription on the target org.

    Returns a dict with seeded IDs/emails for assertions.
    """
    async with factory() as db:
        admin_org = Organization(name="Admin Org", billing_cycle_day=1)
        target = Organization(name="Target Inc", billing_cycle_day=1)
        db.add_all([admin_org, target])
        await db.commit()

        plan = Plan(slug=plan_slug, name=plan_name, features=plan_features)
        db.add(plan)
        await db.commit()

        sub = Subscription(
            org_id=target.id,
            plan_id=plan.id,
            status=SubscriptionStatus.ACTIVE,
            billing_interval=BillingInterval.MONTHLY,
        )
        db.add(sub)
        await db.commit()

        sa = User(
            org_id=admin_org.id, username="root",
            email="root@platform.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER, is_superadmin=True, is_active=True,
            email_verified=True,
        )
        plain = User(
            org_id=target.id, username="t_owner",
            email="t_owner@target.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER, is_superadmin=False, is_active=True,
            email_verified=True,
        )
        db.add_all([sa, plain])
        await db.commit()

        return {
            "admin_user_id": sa.id,
            "admin_email": sa.email,
            "admin_org_id": admin_org.id,
            "target_id": target.id,
            "plain_user_id": plain.id,
            "plan_id": plan.id,
            "plan_slug": plan.slug,
            "plan_name": plan.name,
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


# ── Tests ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_feature_state_returns_all_keys(session_factory):
    """Pro plan with ai.budget=True. Body has plan summary and 4 sorted rows."""
    pro_features = {
        "ai.budget": True,
        "ai.forecast": False,
        "ai.smart_plan": False,
        "ai.autocategorize": False,
    }
    seed = await _seed(
        session_factory,
        plan_slug="pro",
        plan_name="Pro",
        plan_features=pro_features,
    )

    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get(f"/api/v1/admin/orgs/{seed['target_id']}/feature-state")

    assert res.status_code == 200
    body = res.json()

    assert body["plan"] is not None
    assert body["plan"]["slug"] == "pro"
    assert body["plan"]["id"] == seed["plan_id"]
    assert body["plan"]["name"] == "Pro"

    rows = body["features"]
    assert isinstance(rows, list)
    assert len(rows) == 4

    keys = [r["key"] for r in rows]
    assert keys == sorted(ALL_FEATURE_KEYS)
    assert keys == sorted(
        ["ai.budget", "ai.forecast", "ai.smart_plan", "ai.autocategorize"]
    )

    by_key = {r["key"]: r for r in rows}
    assert by_key["ai.budget"]["plan_default"] is True
    assert by_key["ai.budget"]["effective"] is True
    assert by_key["ai.budget"]["override"] is None
    for k in ("ai.forecast", "ai.smart_plan", "ai.autocategorize"):
        assert by_key[k]["plan_default"] is False
        assert by_key[k]["effective"] is False
        assert by_key[k]["override"] is None


@pytest.mark.asyncio
async def test_feature_state_effective_reflects_override(session_factory):
    """Free plan (all-False) + PUT ai.budget=True → effective=True, override block populated."""
    free_features = {
        "ai.budget": False,
        "ai.forecast": False,
        "ai.smart_plan": False,
        "ai.autocategorize": False,
    }
    seed = await _seed(
        session_factory,
        plan_slug="free",
        plan_name="Free",
        plan_features=free_features,
    )

    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        put_res = client.put(
            f"/api/v1/admin/orgs/{seed['target_id']}/feature-overrides/ai.budget",
            json={"value": True, "note": "internal beta"},
        )
        assert put_res.status_code == 200

        res = client.get(f"/api/v1/admin/orgs/{seed['target_id']}/feature-state")

    assert res.status_code == 200
    body = res.json()

    assert body["plan"] is not None
    assert body["plan"]["slug"] == "free"

    by_key = {r["key"]: r for r in body["features"]}

    budget_row = by_key["ai.budget"]
    assert budget_row["plan_default"] is False
    assert budget_row["effective"] is True
    assert budget_row["override"] is not None
    assert budget_row["override"]["value"] is True
    assert budget_row["override"]["set_by_email"] == seed["admin_email"]
    assert budget_row["override"]["set_by"] == seed["admin_user_id"]
    assert budget_row["override"]["is_expired"] is False
    assert budget_row["override"]["feature_key"] == "ai.budget"

    # Other keys unchanged: plan_default=False, no override.
    for k in ("ai.forecast", "ai.smart_plan", "ai.autocategorize"):
        assert by_key[k]["plan_default"] is False
        assert by_key[k]["effective"] is False
        assert by_key[k]["override"] is None


@pytest.mark.asyncio
async def test_feature_state_404_on_missing_org(session_factory):
    """Bogus org_id → 404, not all-False fallback."""
    pro_features = {
        "ai.budget": True,
        "ai.forecast": False,
        "ai.smart_plan": False,
        "ai.autocategorize": False,
    }
    await _seed(
        session_factory,
        plan_slug="pro",
        plan_name="Pro",
        plan_features=pro_features,
    )

    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/orgs/999999/feature-state")

    assert res.status_code == 404


@pytest.mark.asyncio
async def test_feature_state_requires_orgs_view(session_factory):
    """Non-superadmin caller → 403 even on a valid org."""
    pro_features = {
        "ai.budget": True,
        "ai.forecast": False,
        "ai.smart_plan": False,
        "ai.autocategorize": False,
    }
    seed = await _seed(
        session_factory,
        plan_slug="pro",
        plan_name="Pro",
        plan_features=pro_features,
    )

    app = make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.get(f"/api/v1/admin/orgs/{seed['target_id']}/feature-state")

    assert res.status_code == 403
