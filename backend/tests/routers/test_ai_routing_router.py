"""Router tests for the new /routing endpoints (PR1).

Pins:
- GET returns bundle shape (default + features list).
- PUT default + PUT feature + DELETE feature happy paths.
- 400 on cross-org credential_id.
- 400 on unknown feature_name.
- 403 for non-admin.
"""
from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.user import Organization, Role, User
from app.routers.ai_providers import router as ai_providers_router
from app.security import hash_password
from app.services.ai_providers.base import ValidateResult


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
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _set_ai_key(monkeypatch):
    monkeypatch.setattr(
        app_settings,
        "ai_credential_encryption_key",
        base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"),
    )
    monkeypatch.setattr(
        app_settings, "ai_credential_encryption_key_prev", ""
    )


def _make_app(session_factory, resolver):
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user() -> User:
        return await resolver(session_factory)

    def override_get_session_factory():
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_session_factory] = override_get_session_factory
    app.include_router(ai_providers_router)
    return app


async def _seed(factory):
    async with factory() as db:
        org_a = Organization(name="Acme", billing_cycle_day=1)
        org_b = Organization(name="Globex", billing_cycle_day=1)
        db.add_all([org_a, org_b])
        await db.commit()

        owner_a = User(
            org_id=org_a.id,
            username="a_owner",
            email="a_owner@acme.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            is_active=True,
            email_verified=True,
        )
        member_a = User(
            org_id=org_a.id,
            username="a_member",
            email="a_member@acme.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER,
            is_active=True,
            email_verified=True,
        )
        owner_b = User(
            org_id=org_b.id,
            username="b_owner",
            email="b_owner@globex.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            is_active=True,
            email_verified=True,
        )
        db.add_all([owner_a, member_a, owner_b])
        await db.commit()
        return {
            "org_a": org_a.id,
            "org_b": org_b.id,
            "owner_a": owner_a.id,
            "member_a": member_a.id,
            "owner_b": owner_b.id,
        }


async def _get_user(factory, user_id: int) -> User:
    from sqlalchemy import select
    async with factory() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        return result.scalar_one()


def _patch_adapter():
    adapter = type(
        "FakeAdapter",
        (),
        {
            "validate": AsyncMock(
                return_value=ValidateResult(
                    ok=True,
                    discovered_models=["gpt-4o-mini"],
                    discovered_capabilities=["chat"],
                )
            )
        },
    )()
    return patch(
        "app.services.ai_credential_service.get_adapter",
        return_value=adapter,
    )


def _create_cred(client: TestClient, label: str) -> int:
    with _patch_adapter():
        resp = client.post(
            "/api/v1/settings/ai-providers",
            json={
                "provider": "openai",
                "api_key": f"sk-{label}-secret-1234",
                "label": label,
            },
        )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_get_routing_returns_empty_bundle(session_factory):
    ids = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, ids["owner_a"])

    client = TestClient(_make_app(session_factory, resolver))
    resp = client.get("/api/v1/settings/ai-providers/routing")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"default": None, "features": []}


async def test_put_default_routing_then_get(session_factory):
    ids = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, ids["owner_a"])

    client = TestClient(_make_app(session_factory, resolver))
    cred_id = _create_cred(client, "prod")

    resp = client.put(
        "/api/v1/settings/ai-providers/routing/default",
        json={"credential_id": cred_id, "model": "gpt-4o-mini"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["credential_id"] == cred_id
    assert body["model"] == "gpt-4o-mini"

    resp = client.get("/api/v1/settings/ai-providers/routing")
    assert resp.json()["default"]["credential_id"] == cred_id


async def test_put_default_routing_cross_org_rejected(session_factory):
    ids = await _seed(session_factory)

    async def resolver_a(_factory):
        return await _get_user(session_factory, ids["owner_a"])

    async def resolver_b(_factory):
        return await _get_user(session_factory, ids["owner_b"])

    # Create one credential in Org B.
    client_b = TestClient(_make_app(session_factory, resolver_b))
    cred_b_id = _create_cred(client_b, "b-prod")

    # Try to point Org A's default at it.
    client_a = TestClient(_make_app(session_factory, resolver_a))
    resp = client_a.put(
        "/api/v1/settings/ai-providers/routing/default",
        json={"credential_id": cred_b_id, "model": "gpt-4o-mini"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "cross_org_routing_denied"


async def test_put_feature_routing_happy_path(session_factory):
    ids = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, ids["owner_a"])

    client = TestClient(_make_app(session_factory, resolver))
    cred_id = _create_cred(client, "prod")

    resp = client.put(
        "/api/v1/settings/ai-providers/routing/features/categorize_transactions",
        json={"credential_id": cred_id, "model": "gpt-4o"},
    )
    assert resp.status_code == 200
    assert resp.json()["feature_name"] == "categorize_transactions"


async def test_put_feature_routing_rejects_unknown_feature(session_factory):
    ids = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, ids["owner_a"])

    client = TestClient(_make_app(session_factory, resolver))
    cred_id = _create_cred(client, "prod")

    resp = client.put(
        "/api/v1/settings/ai-providers/routing/features/not_a_real_feature",
        json={"credential_id": cred_id, "model": "gpt-4o"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "unknown_feature"


async def test_delete_feature_routing(session_factory):
    ids = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, ids["owner_a"])

    client = TestClient(_make_app(session_factory, resolver))
    cred_id = _create_cred(client, "prod")

    client.put(
        "/api/v1/settings/ai-providers/routing/features/categorize_transactions",
        json={"credential_id": cred_id, "model": "gpt-4o"},
    )
    resp = client.delete(
        "/api/v1/settings/ai-providers/routing/features/categorize_transactions"
    )
    assert resp.status_code == 204
    # Second delete -> 404.
    resp2 = client.delete(
        "/api/v1/settings/ai-providers/routing/features/categorize_transactions"
    )
    assert resp2.status_code == 404


async def test_routing_endpoints_403_for_member(session_factory):
    ids = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, ids["member_a"])

    client = TestClient(_make_app(session_factory, resolver))
    assert client.get("/api/v1/settings/ai-providers/routing").status_code == 403
    assert (
        client.put(
            "/api/v1/settings/ai-providers/routing/default",
            json={"credential_id": 1, "model": "x"},
        ).status_code
        == 403
    )
