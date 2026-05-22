"""Dispatch-layer tests for PR3 capability wrappers.

Covers:
- ``call_llm_embed`` happy path writes a ledger row.
- ``call_llm_structured`` happy path on first attempt records
  ``retries_used=0`` in the ledger row.
- ``call_llm_structured`` retry on JSON parse failure succeeds on
  second attempt with ``retries_used=1``.
- ``call_llm_structured`` exhausts the retry budget after 3 failed
  attempts and raises ``StructuredOutputError``; the failure ledger
  row carries ``retries_used=2`` and
  ``error_class="STATUS_ERROR_STRUCTURED_OUTPUT"``.
- Routing capability mismatch → ``AICapabilityNotSupported`` →
  HTTPException 412 with ``ai_capability_not_supported``.
- ``call_llm_stream`` completes and writes exactly ONE ledger row at
  end-of-stream (not per chunk).
- ``call_llm_function`` happy path on OpenAI-shape tool_calls.
"""
from __future__ import annotations

import base64
import json
import os
from collections.abc import AsyncIterator
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import HTTPException
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
from app.models.org_ai_credential import AiProvider, OrgAICredential
from app.models.org_ai_routing import OrgAIDefaultRouting
from app.models.user import Organization, Role, User
from app.services.ai_credential_crypto import encrypt
from app.services.ai_dispatch import (
    AICapabilityNotSupported,
    call_llm_embed,
    call_llm_function,
    call_llm_stream,
    call_llm_structured,
    http_for_dispatch_error,
)
from app.services.ai_providers.base import (
    EmbedResponse,
    FunctionCallResponse,
    LLMResponse,
    StreamChunk,
    StructuredOutputError,
    TokenUsage,
)


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
        # PR3: capability check requires this list to advertise the
        # capability the call requests.
        discovered_capabilities=[
            "chat",
            "embed",
            "structured_output",
            "function_call",
            "stream",
        ],
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


# ---------- call_llm_embed -------------------------------------------


@pytest.mark.asyncio
async def test_call_llm_embed_happy_path_writes_ledger(
    db: AsyncSession, org, admin_user, credential, default_routing
):
    adapter = MagicMock()
    adapter.embed = AsyncMock(
        return_value=EmbedResponse(
            vectors=[[0.1, 0.2, 0.3]],
            model="text-embedding-3-small",
            prompt_tokens=7,
        )
    )
    with patch(
        "app.services.ai_dispatch.get_adapter", return_value=adapter
    ):
        result = await call_llm_embed(
            db,
            org_id=org.id,
            feature_key="chat",
            texts=["x"],
            model="text-embedding-3-small",
        )
    assert result.response.vectors == [[0.1, 0.2, 0.3]]
    rows = (
        await db.execute(
            select(AIUsageLedger).where(AIUsageLedger.org_id == org.id)
        )
    ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.success is True
    assert row.prompt_tokens == 7
    assert row.completion_tokens == 0
    assert row.model == "text-embedding-3-small"
    assert row.retries_used == 0


# ---------- call_llm_structured retry budget -------------------------


@pytest.mark.asyncio
async def test_call_llm_structured_happy_path_no_retries(
    db: AsyncSession, org, admin_user, credential, default_routing
):
    adapter = MagicMock()
    adapter.chat_structured = AsyncMock(
        return_value=LLMResponse(
            content='{"category": "groceries"}',
            prompt_tokens=5,
            completion_tokens=3,
            model="gpt-4o-mini",
        )
    )
    with patch(
        "app.services.ai_dispatch.get_adapter", return_value=adapter
    ):
        result = await call_llm_structured(
            db,
            org_id=org.id,
            feature_key="chat",
            messages=[{"role": "user", "content": "x"}],
            response_schema={
                "type": "object",
                "required": ["category"],
                "properties": {"category": {"type": "string"}},
            },
        )
    assert result.response.parsed == {"category": "groceries"}
    assert result.response.retries_used == 0
    adapter.chat_structured.assert_awaited_once()
    rows = (
        await db.execute(
            select(AIUsageLedger).where(AIUsageLedger.org_id == org.id)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].retries_used == 0
    assert rows[0].success is True


@pytest.mark.asyncio
async def test_call_llm_structured_retries_once_then_succeeds(
    db: AsyncSession, org, admin_user, credential, default_routing
):
    """First attempt returns malformed JSON; second attempt succeeds.

    ``retries_used`` on the success ledger row = 1.
    """
    adapter = MagicMock()
    adapter.chat_structured = AsyncMock(
        side_effect=[
            LLMResponse(
                content="not json at all",
                prompt_tokens=4,
                completion_tokens=2,
                model="gpt-4o-mini",
            ),
            LLMResponse(
                content='{"category": "rent"}',
                prompt_tokens=4,
                completion_tokens=3,
                model="gpt-4o-mini",
            ),
        ]
    )
    with patch(
        "app.services.ai_dispatch.get_adapter", return_value=adapter
    ):
        result = await call_llm_structured(
            db,
            org_id=org.id,
            feature_key="chat",
            messages=[{"role": "user", "content": "x"}],
            response_schema={
                "type": "object",
                "required": ["category"],
                "properties": {"category": {"type": "string"}},
            },
        )
    assert result.response.retries_used == 1
    assert adapter.chat_structured.await_count == 2
    # Retry system message was injected on attempt #2.
    second_call_kwargs = adapter.chat_structured.await_args_list[1].kwargs
    assert any(
        m.get("role") == "system"
        and "Previous response was not valid JSON" in m.get("content", "")
        for m in second_call_kwargs["messages"]
    )
    # Single ledger row, retries_used=1, success=True.
    rows = (
        await db.execute(
            select(AIUsageLedger).where(AIUsageLedger.org_id == org.id)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].retries_used == 1
    assert rows[0].success is True


@pytest.mark.asyncio
async def test_call_llm_structured_exhausts_retry_budget_raises_typed_error(
    db: AsyncSession, org, admin_user, credential, default_routing
):
    """Three failed parses → ``StructuredOutputError``.

    Architect lock #13: max 2 retries (3 total attempts).
    """
    bad_response = LLMResponse(
        content="still not json",
        prompt_tokens=2,
        completion_tokens=2,
        model="gpt-4o-mini",
    )
    adapter = MagicMock()
    adapter.chat_structured = AsyncMock(
        side_effect=[bad_response, bad_response, bad_response]
    )
    with patch(
        "app.services.ai_dispatch.get_adapter", return_value=adapter
    ):
        with pytest.raises(StructuredOutputError) as exc_info:
            await call_llm_structured(
                db,
                org_id=org.id,
                feature_key="chat",
                messages=[{"role": "user", "content": "x"}],
                response_schema={
                    "type": "object",
                    "required": ["category"],
                    "properties": {"category": {"type": "string"}},
                },
            )
    assert exc_info.value.code == "STATUS_ERROR_STRUCTURED_OUTPUT"
    # Exactly 3 adapter invocations (1 initial + 2 retries).
    assert adapter.chat_structured.await_count == 3
    # Failure ledger row: retries_used=2, success=False.
    rows = (
        await db.execute(
            select(AIUsageLedger).where(AIUsageLedger.org_id == org.id)
        )
    ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.retries_used == 2
    assert row.success is False
    assert row.error_class == "STATUS_ERROR_STRUCTURED_OUTPUT"


@pytest.mark.asyncio
async def test_call_llm_structured_schema_failure_triggers_retry(
    db: AsyncSession, org, admin_user, credential, default_routing
):
    """JSON parses fine but fails schema (missing required key) →
    retry. Pins that schema-validation failure is wired into the
    retry budget, not just JSON-parse failure.
    """
    adapter = MagicMock()
    adapter.chat_structured = AsyncMock(
        side_effect=[
            LLMResponse(
                # Valid JSON but missing the required "category" key.
                content='{"other": "value"}',
                prompt_tokens=2,
                completion_tokens=2,
                model="gpt-4o-mini",
            ),
            LLMResponse(
                content='{"category": "ok"}',
                prompt_tokens=2,
                completion_tokens=2,
                model="gpt-4o-mini",
            ),
        ]
    )
    with patch(
        "app.services.ai_dispatch.get_adapter", return_value=adapter
    ):
        result = await call_llm_structured(
            db,
            org_id=org.id,
            feature_key="chat",
            messages=[{"role": "user", "content": "x"}],
            response_schema={
                "type": "object",
                "required": ["category"],
                "properties": {"category": {"type": "string"}},
            },
        )
    assert result.response.retries_used == 1


# ---------- Routing capability check ---------------------------------


@pytest.mark.asyncio
async def test_routing_capability_mismatch_raises_412(
    db: AsyncSession, org, admin_user, default_routing
):
    """Credential whose ``discovered_capabilities`` lacks ``embed`` ->
    ``AICapabilityNotSupported`` -> HTTP 412 with
    ``ai_capability_not_supported``.
    """
    # Routing already points at a credential. We swap its discovered
    # caps to something that excludes embed.
    cred = (
        await db.execute(select(OrgAICredential))
    ).scalar_one()
    cred.discovered_capabilities = ["chat"]
    await db.commit()

    with pytest.raises(AICapabilityNotSupported) as exc_info:
        await call_llm_embed(
            db,
            org_id=org.id,
            feature_key="chat",
            texts=["x"],
            model="text-embedding-3-small",
        )
    assert exc_info.value.code == "ai_capability_not_supported"
    assert exc_info.value.capability == "embed"
    assert exc_info.value.feature_key == "chat"
    # No ledger row written for the rejected call.
    rows = (
        await db.execute(
            select(AIUsageLedger).where(AIUsageLedger.org_id == org.id)
        )
    ).scalars().all()
    assert rows == []

    # HTTPException mapper surfaces 412 + the helpful message.
    http = http_for_dispatch_error(exc_info.value)
    assert isinstance(http, HTTPException)
    assert http.status_code == 412
    assert http.detail["code"] == "ai_capability_not_supported"
    assert http.detail["capability"] == "embed"
    assert "Reconfigure routing" in http.detail["message"]


# ---------- Function call -------------------------------------------


@pytest.mark.asyncio
async def test_call_llm_function_happy_path(
    db: AsyncSession, org, admin_user, credential, default_routing
):
    adapter = MagicMock()
    adapter.function_call = AsyncMock(
        return_value=FunctionCallResponse(
            tool_calls=[
                {"name": "set_category", "arguments": {"slug": "rent"}}
            ],
            content="",
            prompt_tokens=12,
            completion_tokens=6,
            model="gpt-4o-mini",
        )
    )
    with patch(
        "app.services.ai_dispatch.get_adapter", return_value=adapter
    ):
        result = await call_llm_function(
            db,
            org_id=org.id,
            feature_key="chat",
            messages=[{"role": "user", "content": "classify"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "set_category",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        )
    assert result.response.tool_calls[0]["name"] == "set_category"
    assert result.response.tool_calls[0]["arguments"] == {"slug": "rent"}
    rows = (
        await db.execute(
            select(AIUsageLedger).where(AIUsageLedger.org_id == org.id)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].success is True
    assert rows[0].prompt_tokens == 12
    assert rows[0].completion_tokens == 6


# ---------- Stream: ONE ledger row at end-of-stream ----------------


@pytest.mark.asyncio
async def test_call_llm_stream_writes_one_ledger_row_at_end(
    db: AsyncSession, org, admin_user, credential, default_routing
):
    """Pins the "stream writes ONE row at end, not per chunk"
    invariant. Three delta chunks + one done chunk → one ledger row.
    """
    chunks_sent = [
        StreamChunk(delta_text="Hello ", done=False),
        StreamChunk(delta_text="from ", done=False),
        StreamChunk(delta_text="the model.", done=False),
        StreamChunk(
            delta_text="",
            done=True,
            final_usage=TokenUsage(prompt_tokens=5, completion_tokens=7),
        ),
    ]

    async def fake_stream(*, model, messages, max_tokens=None):
        for c in chunks_sent:
            yield c

    adapter = MagicMock()
    adapter.stream = fake_stream

    with patch(
        "app.services.ai_dispatch.get_adapter", return_value=adapter
    ):
        received = []
        async for chunk in call_llm_stream(
            db,
            org_id=org.id,
            feature_key="chat",
            messages=[{"role": "user", "content": "hi"}],
        ):
            received.append(chunk)
        # 3 deltas + 1 done.
        assert len(received) == 4
        assert received[-1].done is True

    rows = (
        await db.execute(
            select(AIUsageLedger).where(AIUsageLedger.org_id == org.id)
        )
    ).scalars().all()
    assert len(rows) == 1, (
        "stream MUST write exactly one ledger row at end-of-stream, "
        "not one per chunk"
    )
    assert rows[0].success is True
    assert rows[0].prompt_tokens == 5
    assert rows[0].completion_tokens == 7


@pytest.mark.asyncio
async def test_call_llm_stream_falls_back_to_char_estimate_when_no_usage(
    db: AsyncSession, org, admin_user, credential, default_routing
):
    """When the provider doesn't emit final_usage, fall back to
    char/4 on the accumulated text. Pins the estimation contract.
    """
    accumulated = "a" * 40  # 40 chars / 4 = 10 tokens

    async def fake_stream(*, model, messages, max_tokens=None):
        yield StreamChunk(delta_text=accumulated, done=False)
        yield StreamChunk(delta_text="", done=True, final_usage=None)

    adapter = MagicMock()
    adapter.stream = fake_stream

    with patch(
        "app.services.ai_dispatch.get_adapter", return_value=adapter
    ):
        async for _ in call_llm_stream(
            db,
            org_id=org.id,
            feature_key="chat",
            messages=[],
        ):
            pass

    rows = (
        await db.execute(
            select(AIUsageLedger).where(AIUsageLedger.org_id == org.id)
        )
    ).scalars().all()
    assert len(rows) == 1
    # Estimated 10 completion tokens (40 chars / 4).
    assert rows[0].completion_tokens == 10
