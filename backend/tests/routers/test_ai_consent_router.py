"""Router tests for the new /consent endpoints (PR1)."""
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
        return {"org": org.id, "owner": owner.id}


async def _get_user(factory, user_id: int) -> User:
    from sqlalchemy import select
    async with factory() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        return result.scalar_one()


async def test_get_consent_default_snapshot_is_all_false(session_factory):
    ids = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, ids["owner"])

    client = TestClient(_make_app(session_factory, resolver))
    resp = client.get("/api/v1/settings/ai-providers/consent")
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_consent"] is False
    assert body["allow_training"] is False
    assert body["allow_rag"] is False
    assert body["allow_telemetry"] is False
    assert body["consent_version"] is None


async def test_post_consent_then_get_reflects_latest(session_factory):
    ids = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, ids["owner"])

    client = TestClient(_make_app(session_factory, resolver))
    resp = client.post(
        "/api/v1/settings/ai-providers/consent",
        json={
            "consent_version": "ai-tos-2026-05-22",
            "allow_training": True,
            "allow_rag": False,
            "allow_telemetry": True,
        },
    )
    assert resp.status_code == 201, resp.text

    get_resp = client.get("/api/v1/settings/ai-providers/consent")
    body = get_resp.json()
    assert body["has_consent"] is True
    assert body["allow_training"] is True
    assert body["allow_rag"] is False
    assert body["allow_telemetry"] is True
    assert body["consent_version"] == "ai-tos-2026-05-22"


async def test_revocation_row_flips_snapshot(session_factory):
    ids = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, ids["owner"])

    client = TestClient(_make_app(session_factory, resolver))
    client.post(
        "/api/v1/settings/ai-providers/consent",
        json={
            "consent_version": "ai-tos-2026-05-22",
            "allow_training": True,
            "allow_rag": True,
            "allow_telemetry": True,
        },
    )
    client.post(
        "/api/v1/settings/ai-providers/consent",
        json={
            "consent_version": "ai-tos-2026-05-22",
            "revoked": True,
        },
    )
    body = client.get("/api/v1/settings/ai-providers/consent").json()
    assert body["has_consent"] is False
    assert body["allow_training"] is False
