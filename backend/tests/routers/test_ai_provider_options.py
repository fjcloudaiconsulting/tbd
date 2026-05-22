"""Provider-options endpoint + native-rejection via the router (PR1).

Pins:
- ``/options`` lists 5 providers; native is the only one with
  ``availability != 'available'`` until AI_NATIVE_ENABLED flips on.
- POSTing a credential with provider=native always returns
  ``native_not_available`` regardless of the gate.
"""
from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator

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
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        owner = User(
            org_id=org.id,
            username="owner",
            email="owner@acme.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            is_active=True,
            email_verified=True,
        )
        db.add(owner)
        await db.commit()
        return owner.id


async def _get_user(factory, user_id: int) -> User:
    from sqlalchemy import select
    async with factory() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        return result.scalar_one()


async def test_options_endpoint_lists_five_providers_native_not_yet(
    session_factory, monkeypatch
):
    monkeypatch.setattr(app_settings, "ai_native_enabled", False)
    owner_id = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, owner_id)

    client = TestClient(_make_app(session_factory, resolver))
    resp = client.get("/api/v1/settings/ai-providers/options")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ai_native_enabled"] is False
    keys = {p["key"]: p for p in body["providers"]}
    assert set(keys) == {
        "openai", "anthropic", "ollama", "openai_compatible", "native",
    }
    assert keys["native"]["availability"] == "not_yet_available"
    assert keys["openai"]["availability"] == "available"


async def test_options_returns_not_yet_available_for_native_when_flag_true(
    session_factory, monkeypatch
):
    """Architect-locked: /options must NOT lie when the gate flips on.

    PR1 ships no native backend, so the create path always refuses
    native regardless of ``AI_NATIVE_ENABLED``. The /options endpoint
    must match — advertising ``available`` while creation rejects would
    be a UI lie. The env flag is reported back for visibility only.
    """
    monkeypatch.setattr(app_settings, "ai_native_enabled", True)
    owner_id = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, owner_id)

    client = TestClient(_make_app(session_factory, resolver))
    resp = client.get("/api/v1/settings/ai-providers/options")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ai_native_enabled"] is True
    keys = {p["key"]: p for p in body["providers"]}
    assert keys["native"]["availability"] == "not_yet_available"


async def test_create_credential_with_native_provider_rejected(session_factory):
    owner_id = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, owner_id)

    client = TestClient(_make_app(session_factory, resolver))
    resp = client.post(
        "/api/v1/settings/ai-providers",
        json={
            "provider": "native",
            "api_key": "placeholder-but-this-must-still-refuse",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "native_not_available"


async def test_create_credential_with_native_rejected_even_when_gate_on(
    session_factory, monkeypatch
):
    """Architect-locked: PR1 backend isn't ready even when gate flips on."""
    monkeypatch.setattr(app_settings, "ai_native_enabled", True)
    owner_id = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, owner_id)

    client = TestClient(_make_app(session_factory, resolver))
    resp = client.post(
        "/api/v1/settings/ai-providers",
        json={
            "provider": "native",
            "api_key": "placeholder",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "native_not_available"
