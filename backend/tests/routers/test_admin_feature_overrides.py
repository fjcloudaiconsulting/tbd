"""Router tests for L4.11 admin feature-override PUT endpoint.

`PUT /api/v1/admin/orgs/{org_id}/feature-overrides/{feature_key}`
upserts a per-org boolean override and emits a structured
`admin.org.feature.set` audit event. orgs.manage gates the endpoint
(superadmin short-circuits in the current permission scheme). Note
text never lands in the audit payload, only `note_present` does.

DELETE coverage lives in `test_admin_feature_overrides_delete.py`
(T15) and aggregate state coverage in `test_admin_feature_state.py`
(T16) — keep this file PUT-only.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import patch

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
    one we'll set overrides on)."""
    async with factory() as db:
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


# ── PUT happy-paths ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_sets_new_override(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with patch("app.routers.admin_orgs.log") as mock_log:
        with TestClient(app) as client:
            res = client.put(
                f"/api/v1/admin/orgs/{seed['target_id']}/feature-overrides/ai.budget",
                json={"value": True, "note": "internal beta"},
            )
    assert res.status_code == 200
    body = res.json()
    assert body["feature_key"] == "ai.budget"
    assert body["value"] is True
    assert body["set_by"] == seed["admin_user_id"]
    assert body["set_by_email"] == seed["admin_email"]
    assert body["is_expired"] is False

    mock_log.info.assert_called_once()
    args, kwargs = mock_log.info.call_args
    assert args[0] == "admin.org.feature.set"
    assert kwargs["target_org_id"] == seed["target_id"]
    assert kwargs["feature_key"] == "ai.budget"
    assert kwargs["old_value"] is None
    assert kwargs["new_value"] is True
    assert kwargs["note_present"] is True
    assert kwargs["actor_email"] == seed["admin_email"]
    # Note text NEVER lands in the audit payload.
    assert "note" not in kwargs or kwargs.get("note") is None
    assert "internal beta" not in str(kwargs.values())


@pytest.mark.asyncio
async def test_put_updates_existing_override(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        first = client.put(
            f"/api/v1/admin/orgs/{seed['target_id']}/feature-overrides/ai.forecast",
            json={"value": True},
        )
        assert first.status_code == 200
        assert first.json()["value"] is True

        with patch("app.routers.admin_orgs.log") as mock_log:
            second = client.put(
                f"/api/v1/admin/orgs/{seed['target_id']}/feature-overrides/ai.forecast",
                json={"value": False, "note": "rolled back"},
            )

    assert second.status_code == 200
    body = second.json()
    assert body["value"] is False
    assert body["set_by_email"] == seed["admin_email"]

    mock_log.info.assert_called_once()
    args, kwargs = mock_log.info.call_args
    assert args[0] == "admin.org.feature.set"
    assert kwargs["old_value"] is True
    assert kwargs["new_value"] is False
    assert kwargs["note_present"] is True


# ── PUT validation rejections ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_rejects_unknown_key(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/admin/orgs/{seed['target_id']}/feature-overrides/ai.totally_made_up",
            json={"value": True},
        )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_put_strict_bool_rejects_string(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/admin/orgs/{seed['target_id']}/feature-overrides/ai.budget",
            json={"value": "true"},
        )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_put_extra_fields_rejected(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/admin/orgs/{seed['target_id']}/feature-overrides/ai.budget",
            json={"value": True, "extra": "x"},
        )
    assert res.status_code == 422


# ── auth gate ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_requires_orgs_manage(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/admin/orgs/{seed['target_id']}/feature-overrides/ai.budget",
            json={"value": True},
        )
    assert res.status_code == 403


# ── DELETE ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_revokes_existing_override(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        put_res = client.put(
            f"/api/v1/admin/orgs/{seed['target_id']}/feature-overrides/ai.budget",
            json={"value": True},
        )
        assert put_res.status_code == 200

        with patch("app.routers.admin_orgs.log") as mock_log:
            del_res = client.delete(
                f"/api/v1/admin/orgs/{seed['target_id']}/feature-overrides/ai.budget",
            )

    assert del_res.status_code == 204
    assert del_res.content == b""

    mock_log.info.assert_called_once()
    args, kwargs = mock_log.info.call_args
    assert args[0] == "admin.org.feature.revoked"
    assert kwargs["target_org_id"] == seed["target_id"]
    assert kwargs["feature_key"] == "ai.budget"
    assert kwargs["old_value"] is True
    assert kwargs["actor_user_id"] == seed["admin_user_id"]
    assert kwargs["actor_email"] == seed["admin_email"]


@pytest.mark.asyncio
async def test_delete_returns_404_when_no_override(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.delete(
            f"/api/v1/admin/orgs/{seed['target_id']}/feature-overrides/ai.budget",
        )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_delete_unknown_key_returns_400(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.delete(
            f"/api/v1/admin/orgs/{seed['target_id']}/feature-overrides/ai.totally_made_up",
        )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_delete_requires_orgs_manage(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.delete(
            f"/api/v1/admin/orgs/{seed['target_id']}/feature-overrides/ai.budget",
        )
    assert res.status_code == 403
