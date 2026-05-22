"""Dispatch service tests (PR2 of AI tier train).

Pins the ``call_llm`` chokepoint:

- Happy path writes a ledger row.
- Missing routing raises ``NoRoutingConfigured``.
- Hard cap raises ``AICapExceeded``; the adapter is NEVER called and
  NO ledger row is written for the rejected call.
- Soft cap crossing first time -> notification dispatched + Redis
  marker set; second call same period does NOT re-dispatch.
- Adapter failure -> ledger row with success=false, error_class set,
  exception re-raised as AIDispatchFailed.
- Feature cap tighter than default -> feature cap wins.
- Default cap tighter than feature cap -> default cap wins.
"""
from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Optional
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
from app.models.ai_usage_ledger import AIUsageLedger
from app.models.org_ai_caps import OrgAIDefaultCaps, OrgAIFeatureCaps
from app.models.org_ai_credential import AiProvider, OrgAICredential
from app.models.org_ai_routing import (
    OrgAIDefaultRouting,
    OrgAIFeatureRouting,
)
from app.models.user import Organization, Role, User
from app.services import ai_dispatch
from app.services.ai_credential_crypto import encrypt
from app.services.ai_dispatch import (
    AICapExceeded,
    AIDispatchFailed,
    NoRoutingConfigured,
    call_llm,
)
from app.services.ai_providers.base import AIProviderError, LLMResponse


# ---------- fixtures --------------------------------------------------


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
    monkeypatch.setattr(
        app_settings, "ai_credential_encryption_key_prev", ""
    )


@pytest.fixture(autouse=True)
def _stub_redis(monkeypatch):
    """Default: Redis disabled so the soft-cap path falls through to
    "warn every call". Tests that care about marker behavior install
    their own MagicMock client.
    """
    monkeypatch.setattr(
        "app.services.ai_dispatch.redis_client.get_client",
        lambda: None,
    )


@pytest_asyncio.fixture
async def org(db: AsyncSession) -> Organization:
    org = Organization(name="Acme", billing_cycle_day=1)
    db.add(org)
    await db.commit()
    return org


@pytest_asyncio.fixture
async def admin_user(db: AsyncSession, org: Organization) -> User:
    user = User(
        org_id=org.id,
        username="admin",
        email="admin@example.com",
        password_hash="x" * 64,
        role=Role.OWNER,
    )
    db.add(user)
    await db.commit()
    return user


@pytest_asyncio.fixture
async def credential(db: AsyncSession, org: Organization) -> OrgAICredential:
    cred = OrgAICredential(
        org_id=org.id,
        provider=AiProvider.OPENAI,
        encrypted_api_key=encrypt("sk-test-12345"),
        encrypted_bearer_token=None,
        base_url=None,
        key_fingerprint="0123456789abcdef",
        last_four="2345",
        label="primary",
    )
    db.add(cred)
    await db.commit()
    return cred


@pytest_asyncio.fixture
async def default_routing(
    db: AsyncSession, org: Organization, credential: OrgAICredential
) -> OrgAIDefaultRouting:
    row = OrgAIDefaultRouting(
        org_id=org.id, credential_id=credential.id, model="gpt-4o-mini"
    )
    db.add(row)
    await db.commit()
    return row


def _make_adapter(response: Optional[LLMResponse] = None, raises=None):
    """Build an adapter mock with an async chat() coroutine."""
    adapter = MagicMock()
    if raises is not None:
        adapter.chat = AsyncMock(side_effect=raises)
    else:
        adapter.chat = AsyncMock(
            return_value=response
            or LLMResponse(
                content="hi", prompt_tokens=10, completion_tokens=5, model="gpt-4o-mini"
            )
        )
    return adapter


# ---------- happy path ------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_writes_ledger_row(
    db: AsyncSession, org: Organization, admin_user, credential, default_routing
):
    adapter = _make_adapter(
        LLMResponse(
            content="hello", prompt_tokens=42, completion_tokens=8, model="gpt-4o-mini"
        )
    )
    with patch(
        "app.services.ai_dispatch.get_adapter", return_value=adapter
    ):
        result = await call_llm(
            db,
            org_id=org.id,
            feature_key="chat",
            request_payload={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert result.response.content == "hello"
    adapter.chat.assert_awaited_once()
    # Ledger row written.
    rows = (
        await db.execute(select(AIUsageLedger).where(AIUsageLedger.org_id == org.id))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].success is True
    assert rows[0].prompt_tokens == 42
    assert rows[0].completion_tokens == 8
    assert rows[0].total_tokens == 50
    assert rows[0].feature_key == "chat"
    assert rows[0].model == "gpt-4o-mini"
    assert rows[0].error_class is None
    assert rows[0].est_cost_cents >= 1  # nonzero from pricing table


# ---------- no routing ------------------------------------------------


@pytest.mark.asyncio
async def test_no_routing_raises_412(
    db: AsyncSession, org: Organization, admin_user
):
    with pytest.raises(NoRoutingConfigured) as exc_info:
        await call_llm(
            db,
            org_id=org.id,
            feature_key="chat",
            request_payload={"messages": []},
        )
    assert exc_info.value.code == "ai_routing_not_configured"


# ---------- hard cap --------------------------------------------------


@pytest.mark.asyncio
async def test_hard_cap_blocks_dispatch_no_adapter_call_no_ledger_row(
    db: AsyncSession, org: Organization, admin_user, credential, default_routing
):
    # Default cap: hard=10 cents.
    db.add(
        OrgAIDefaultCaps(
            org_id=org.id, soft_cap_cents=None, hard_cap_cents=10, period="monthly"
        )
    )
    # Pre-existing ledger row brings the total to 12 cents already.
    db.add(
        AIUsageLedger(
            org_id=org.id,
            credential_id=credential.id,
            feature_key="chat",
            model="gpt-4o-mini",
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            est_cost_cents=12,
            latency_ms=1,
            success=True,
            error_class=None,
            dispatched_at=datetime.utcnow(),
        )
    )
    await db.commit()

    adapter = _make_adapter()
    with patch(
        "app.services.ai_dispatch.get_adapter", return_value=adapter
    ):
        with pytest.raises(AICapExceeded) as exc_info:
            await call_llm(
                db,
                org_id=org.id,
                feature_key="chat",
                request_payload={"messages": []},
            )
    assert exc_info.value.code == "ai_hard_cap_exceeded"
    adapter.chat.assert_not_awaited()

    # Still just the one pre-existing ledger row — no row for the
    # rejected call.
    rows = (
        await db.execute(select(AIUsageLedger).where(AIUsageLedger.org_id == org.id))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].est_cost_cents == 12


# ---------- adapter failure ------------------------------------------


@pytest.mark.asyncio
async def test_adapter_failure_writes_failed_ledger_row_and_reraises(
    db: AsyncSession, org: Organization, admin_user, credential, default_routing
):
    adapter = _make_adapter(
        raises=AIProviderError(code="provider_status_500", status_code=500)
    )
    with patch(
        "app.services.ai_dispatch.get_adapter", return_value=adapter
    ):
        with pytest.raises(AIDispatchFailed) as exc_info:
            await call_llm(
                db,
                org_id=org.id,
                feature_key="chat",
                request_payload={"messages": []},
            )
    assert exc_info.value.code == "provider_status_500"
    rows = (
        await db.execute(select(AIUsageLedger).where(AIUsageLedger.org_id == org.id))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].success is False
    assert rows[0].error_class == "provider_status_500"
    assert rows[0].prompt_tokens == 0
    assert rows[0].completion_tokens == 0
    assert rows[0].est_cost_cents == 0


# ---------- soft cap notification + Redis dedupe ---------------------


@pytest.mark.asyncio
async def test_soft_cap_dispatches_notification_once_per_period(
    db: AsyncSession, org: Organization, admin_user, credential, default_routing, monkeypatch
):
    # Default soft cap = 5 cents; pre-seed 6 cents so the next call
    # crosses it.
    db.add(
        OrgAIDefaultCaps(
            org_id=org.id, soft_cap_cents=5, hard_cap_cents=None, period="monthly"
        )
    )
    db.add(
        AIUsageLedger(
            org_id=org.id,
            credential_id=credential.id,
            feature_key="chat",
            model="gpt-4o-mini",
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            est_cost_cents=6,
            latency_ms=1,
            success=True,
            error_class=None,
            dispatched_at=datetime.utcnow(),
        )
    )
    await db.commit()

    # Fake redis client — SET NX returns True first time, False second.
    redis_state: dict[str, str] = {}

    class FakeRedis:
        async def set(self, key, value, ex=None, nx=False):
            if nx and key in redis_state:
                return False
            redis_state[key] = value
            return True

    fake = FakeRedis()
    monkeypatch.setattr(
        "app.services.ai_dispatch.redis_client.get_client", lambda: fake
    )

    dispatch_mock = AsyncMock(side_effect=lambda *a, **k: None)
    monkeypatch.setattr(
        "app.services.ai_dispatch.notification_service.dispatch_notification",
        dispatch_mock,
    )

    adapter = _make_adapter()
    with patch(
        "app.services.ai_dispatch.get_adapter", return_value=adapter
    ):
        await call_llm(
            db,
            org_id=org.id,
            feature_key="chat",
            request_payload={"messages": []},
        )
    # First call dispatched a notification to the org admin.
    assert dispatch_mock.await_count == 1
    # Redis marker set.
    assert any(k.startswith("ai_soft_cap_warned:") for k in redis_state)

    # Second call same period -> no new notification.
    with patch(
        "app.services.ai_dispatch.get_adapter", return_value=adapter
    ):
        await call_llm(
            db,
            org_id=org.id,
            feature_key="chat",
            request_payload={"messages": []},
        )
    assert dispatch_mock.await_count == 1


# ---------- cap resolution ------------------------------------------


@pytest.mark.asyncio
async def test_feature_cap_tighter_than_default_wins(
    db: AsyncSession, org: Organization, admin_user, credential, default_routing
):
    # Default hard cap = 1000, feature hard cap = 10.
    db.add(
        OrgAIDefaultCaps(
            org_id=org.id, soft_cap_cents=None, hard_cap_cents=1000, period="monthly"
        )
    )
    db.add(
        OrgAIFeatureCaps(
            org_id=org.id,
            feature_key="chat",
            soft_cap_cents=None,
            hard_cap_cents=10,
            period="monthly",
        )
    )
    # Ledger total 15 — over feature cap (10), under default (1000).
    db.add(
        AIUsageLedger(
            org_id=org.id,
            credential_id=credential.id,
            feature_key="chat",
            model="gpt-4o-mini",
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            est_cost_cents=15,
            latency_ms=1,
            success=True,
            error_class=None,
            dispatched_at=datetime.utcnow(),
        )
    )
    await db.commit()

    adapter = _make_adapter()
    with patch(
        "app.services.ai_dispatch.get_adapter", return_value=adapter
    ):
        with pytest.raises(AICapExceeded):
            await call_llm(
                db,
                org_id=org.id,
                feature_key="chat",
                request_payload={"messages": []},
            )
    adapter.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_cap_tighter_than_feature_cap_wins(
    db: AsyncSession, org: Organization, admin_user, credential, default_routing
):
    # Default hard = 10, feature hard = 1000.
    db.add(
        OrgAIDefaultCaps(
            org_id=org.id, soft_cap_cents=None, hard_cap_cents=10, period="monthly"
        )
    )
    db.add(
        OrgAIFeatureCaps(
            org_id=org.id,
            feature_key="chat",
            soft_cap_cents=None,
            hard_cap_cents=1000,
            period="monthly",
        )
    )
    db.add(
        AIUsageLedger(
            org_id=org.id,
            credential_id=credential.id,
            feature_key="chat",
            model="gpt-4o-mini",
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            est_cost_cents=15,
            latency_ms=1,
            success=True,
            error_class=None,
            dispatched_at=datetime.utcnow(),
        )
    )
    await db.commit()

    adapter = _make_adapter()
    with patch(
        "app.services.ai_dispatch.get_adapter", return_value=adapter
    ):
        with pytest.raises(AICapExceeded):
            await call_llm(
                db,
                org_id=org.id,
                feature_key="chat",
                request_payload={"messages": []},
            )
    adapter.chat.assert_not_awaited()


# ---------- feature routing wins over default ------------------------


@pytest.mark.asyncio
async def test_feature_routing_wins_over_default(
    db: AsyncSession, org: Organization, admin_user, credential, default_routing
):
    # A second credential pinned via a feature routing override.
    other = OrgAICredential(
        org_id=org.id,
        provider=AiProvider.OPENAI,
        encrypted_api_key=encrypt("sk-other"),
        encrypted_bearer_token=None,
        base_url=None,
        key_fingerprint="abcdef0123456789",
        last_four="ther",
        label="override",
    )
    db.add(other)
    await db.commit()
    db.add(
        OrgAIFeatureRouting(
            org_id=org.id,
            feature_name="chat",
            credential_id=other.id,
            model="claude-haiku-4-5",
        )
    )
    await db.commit()

    adapter = _make_adapter()
    captured = {}

    def _capture_factory(provider, *, api_key, bearer_token=None, base_url=None):
        captured["api_key"] = api_key
        return adapter

    with patch(
        "app.services.ai_dispatch.get_adapter", side_effect=_capture_factory
    ):
        await call_llm(
            db,
            org_id=org.id,
            feature_key="chat",
            request_payload={"messages": []},
        )
    assert captured["api_key"] == "sk-other"
    # Model from the feature row was passed through.
    adapter.chat.assert_awaited_once()
    kwargs = adapter.chat.await_args.kwargs
    assert kwargs["model"] == "claude-haiku-4-5"
