"""Provider-options endpoint (PR1, TBD-584).

Pins:
- ``/options`` lists exactly the provider registry: TBD-584 removed
  ``ollama`` (bring-your-own local model) and the ``native`` stub.
- POSTing a credential with a removed provider is a 422, whatever
  ``AI_NATIVE_ENABLED`` says.
"""
from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
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

    async def override_current_user(request: Request) -> User:
        request.state.auth_method = "jwt"  # interactive session (TBD-626)
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


@pytest.mark.parametrize("gate", [False, True])
async def test_options_endpoint_lists_the_provider_registry(
    session_factory, monkeypatch, gate
):
    monkeypatch.setattr(app_settings, "ai_native_enabled", gate)
    owner_id = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, owner_id)

    client = TestClient(_make_app(session_factory, resolver))
    resp = client.get("/api/v1/settings/ai-providers/options")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ai_native_enabled"] is gate
    assert body["providers"] == [
        {"key": "openai", "label": "OpenAI", "availability": "available"},
        {"key": "anthropic", "label": "Anthropic", "availability": "available"},
        {"key": "openai_compatible", "label": "OpenAI-compatible", "availability": "available"},
    ]
    # TBD-590: OpenRouter and Gemini are openai_compatible presets.
    assert body["presets"] == [
        {
            "key": "openrouter",
            "label": "OpenRouter",
            "base_url": "https://openrouter.ai/api/v1",
            "provider": "openai_compatible",
        },
        {
            "key": "gemini",
            "label": "Google Gemini",
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "provider": "openai_compatible",
        },
    ]


@pytest.mark.parametrize("gate", [False, True])
@pytest.mark.parametrize("provider", ["native", "ollama"])
async def test_create_credential_with_a_removed_provider_is_422(
    session_factory, monkeypatch, gate, provider
):
    monkeypatch.setattr(app_settings, "ai_native_enabled", gate)
    owner_id = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, owner_id)

    client = TestClient(_make_app(session_factory, resolver))
    resp = client.post(
        "/api/v1/settings/ai-providers",
        json={
            "provider": provider,
            "api_key": "placeholder-but-this-must-still-refuse",
            "base_url": "https://llm.example.com",
        },
    )
    assert resp.status_code == 422
