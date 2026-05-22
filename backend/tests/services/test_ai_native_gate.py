"""Native gate behavior (PR1 contract).

The full native + consent scaffolding ships in PR1 so PR4 only has to
flip ``AI_NATIVE_ENABLED=true`` and ship a real backend, not refactor
the registry. Until that backend exists:

- Credential creation for ``provider=native`` is always rejected with
  HTTP 400 / ``native_not_available``, regardless of the gate.
- ``NativeAdapter.validate()`` always raises ``NativeNotAvailable``.
- The gate-on branch logs a warning so operators see "gate is on but
  the backend isn't ready" instead of getting silent 500s.
"""
from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.models import Base
from app.models.org_ai_credential import AiProvider
from app.models.user import Organization
from app.schemas.org_ai_credential import OrgAICredentialCreate
from app.services import ai_credential_service
from app.services.ai_providers import NativeNotAvailable, get_adapter
from app.services.ai_providers.native import NativeAdapter


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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


@pytest_asyncio.fixture
async def db(session_factory):
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def org(db: AsyncSession) -> Organization:
    org = Organization(name="Acme", billing_cycle_day=1)
    db.add(org)
    await db.commit()
    return org


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


_ACTOR = {
    "actor_user_id": 1,
    "actor_email": "actor@example.test",
    "request_id": "rid",
    "ip_address": "127.0.0.1",
}


async def test_native_credential_create_rejected_gate_off(
    db, session_factory, org, monkeypatch
):
    monkeypatch.setattr(app_settings, "ai_native_enabled", False)
    payload = OrgAICredentialCreate(
        provider=AiProvider.NATIVE,
        api_key="placeholder",
    )
    with pytest.raises(HTTPException) as exc:
        await ai_credential_service.create_credential(
            db,
            org_id=org.id,
            payload=payload,
            session_factory=session_factory,
            **_ACTOR,
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "native_not_available"


async def test_native_credential_create_rejected_even_when_gate_on(
    db, session_factory, org, monkeypatch
):
    """PR1 still refuses native even with the gate flipped on — the
    backend doesn't exist yet."""
    monkeypatch.setattr(app_settings, "ai_native_enabled", True)
    payload = OrgAICredentialCreate(
        provider=AiProvider.NATIVE,
        api_key="placeholder",
    )
    with pytest.raises(HTTPException) as exc:
        await ai_credential_service.create_credential(
            db,
            org_id=org.id,
            payload=payload,
            session_factory=session_factory,
            **_ACTOR,
        )
    assert exc.value.detail["code"] == "native_not_available"


@pytest.mark.asyncio
async def test_native_adapter_raises_native_not_available(monkeypatch):
    monkeypatch.setattr(app_settings, "ai_native_enabled", False)
    adapter = NativeAdapter()
    with pytest.raises(NativeNotAvailable) as exc:
        await adapter.validate()
    assert exc.value.code == "not_yet_available"


@pytest.mark.asyncio
async def test_native_adapter_raises_even_when_gate_on(monkeypatch):
    monkeypatch.setattr(app_settings, "ai_native_enabled", True)
    adapter = NativeAdapter()
    with pytest.raises(NativeNotAvailable):
        await adapter.validate()


def test_get_adapter_returns_native_adapter():
    adapter = get_adapter(AiProvider.NATIVE, api_key="x")
    assert isinstance(adapter, NativeAdapter)
