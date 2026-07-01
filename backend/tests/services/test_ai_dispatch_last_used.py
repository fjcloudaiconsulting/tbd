"""``_touch_last_used`` best-effort credential telemetry.

Pins the last_used_at refresh hook that fires (fire-and-forget) from
every dispatch success path:

- A successful touch stamps ``last_used_at`` on the right credential
  row, in its OWN isolated session (never the caller's).
- A failure inside the touch is swallowed + logged and NEVER
  propagates — last_used_at is telemetry, not correctness, so a
  broken touch must not fail the AI call.
"""
from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.models import Base
from app.models.org_ai_credential import AiProvider, OrgAICredential
from app.models.org_ai_routing import OrgAIDefaultRouting
from app.models.user import Organization
from app.services import ai_dispatch
from app.services.ai_credential_crypto import encrypt
from app.services.ai_dispatch import _touch_last_used, call_llm
from app.services.ai_providers.base import LLMResponse


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


@pytest.fixture(autouse=True)
def _set_ai_key(monkeypatch):
    monkeypatch.setattr(
        app_settings,
        "ai_credential_encryption_key",
        base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"),
    )
    monkeypatch.setattr(app_settings, "ai_credential_encryption_key_prev", "")


@pytest.fixture(autouse=True)
def _stub_redis(monkeypatch):
    monkeypatch.setattr(
        "app.services.ai_dispatch.redis_client.get_client",
        lambda: None,
    )


@pytest.fixture(autouse=True)
def _isolated_touch_factory(monkeypatch, session_factory):
    """Point the fire-and-forget touch at the in-memory test DB.

    ``_touch_last_used`` opens its OWN session from the module-global
    ``async_session`` (the production MySQL factory). Redirect it to the
    test factory so the touch writes to the same sqlite DB the fixtures
    seed.
    """
    monkeypatch.setattr(ai_dispatch, "async_session", session_factory)


@pytest_asyncio.fixture
async def org(db: AsyncSession) -> Organization:
    org = Organization(name="Acme", billing_cycle_day=1)
    db.add(org)
    await db.commit()
    return org


@pytest_asyncio.fixture
async def credential(db: AsyncSession, org: Organization) -> OrgAICredential:
    cred = OrgAICredential(
        org_id=org.id,
        provider=AiProvider.OPENAI,
        encrypted_api_key=encrypt("sk-test-12345"),
        key_fingerprint="0123456789abcdef",
        last_four="2345",
        label="primary",
    )
    db.add(cred)
    await db.commit()
    return cred


@pytest_asyncio.fixture
async def default_routing(db, org, credential) -> OrgAIDefaultRouting:
    row = OrgAIDefaultRouting(
        org_id=org.id, credential_id=credential.id, model="gpt-4o-mini"
    )
    db.add(row)
    await db.commit()
    return row


# ---------- (a) successful touch stamps last_used_at ------------------


@pytest.mark.asyncio
async def test_touch_last_used_stamps_timestamp(
    db: AsyncSession, credential: OrgAICredential
):
    assert credential.last_used_at is None

    await _touch_last_used(credential.id)

    fresh = (
        await db.execute(
            select(OrgAICredential).where(OrgAICredential.id == credential.id)
        )
    ).scalar_one()
    assert fresh.last_used_at is not None


@pytest.mark.asyncio
async def test_touch_last_used_only_targets_its_credential(
    db: AsyncSession, org: Organization, credential: OrgAICredential
):
    other = OrgAICredential(
        org_id=org.id,
        provider=AiProvider.ANTHROPIC,
        encrypted_api_key=encrypt("sk-other"),
        key_fingerprint="fedcba9876543210",
        last_four="9999",
        label="secondary",
    )
    db.add(other)
    await db.commit()

    await _touch_last_used(credential.id)

    rows = {
        c.id: c.last_used_at
        for c in (
            await db.execute(select(OrgAICredential))
        ).scalars().all()
    }
    assert rows[credential.id] is not None
    assert rows[other.id] is None


# ---------- (b) failure inside the touch never propagates -------------


@pytest.mark.asyncio
async def test_touch_last_used_swallows_write_failure(caplog):
    """A raising session must NOT propagate out of the touch."""

    class _Boom:
        async def __aenter__(self):
            raise RuntimeError("db is down")

        async def __aexit__(self, *exc):
            return False

    with patch.object(ai_dispatch, "async_session", lambda: _Boom()):
        # Must not raise.
        await _touch_last_used(12345)


@pytest.mark.asyncio
async def test_dispatch_succeeds_even_if_touch_write_raises(
    db: AsyncSession, org: Organization, credential, default_routing
):
    """A broken last_used_at write must not fail the AI call."""
    adapter = MagicMock()
    adapter.chat = AsyncMock(
        return_value=LLMResponse(
            content="hello",
            prompt_tokens=10,
            completion_tokens=5,
            model="gpt-4o-mini",
        )
    )

    async def _raise(_credential_id):
        raise RuntimeError("touch exploded")

    with patch(
        "app.services.ai_dispatch.get_adapter", return_value=adapter
    ), patch.object(ai_dispatch, "_touch_last_used", _raise):
        result = await call_llm(
            db,
            org_id=org.id,
            feature_key="chat",
            request_payload={
                "messages": [{"role": "user", "content": "hi"}]
            },
        )

    # Dispatch still succeeds; the fire-and-forget touch failure is
    # contained to its own task.
    assert result.response.content == "hello"
    adapter.chat.assert_awaited_once()
