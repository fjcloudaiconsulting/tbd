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
            dispatched_at=datetime.now(timezone.utc).replace(tzinfo=None),
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


# ---------- projected-overspend gate (call_llm entry point) ----------


async def _seed_cap_and_spend(
    db: AsyncSession,
    org: Organization,
    credential: OrgAICredential,
    *,
    hard_cap_cents: int,
    spent_cents: int,
) -> None:
    """Configure a hard cap and seed prior spend for the org."""
    db.add(
        OrgAIDefaultCaps(
            org_id=org.id,
            soft_cap_cents=None,
            hard_cap_cents=hard_cap_cents,
            period="monthly",
        )
    )
    if spent_cents > 0:
        db.add(
            AIUsageLedger(
                org_id=org.id,
                credential_id=credential.id,
                feature_key="chat",
                model="gpt-4o-mini",
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                est_cost_cents=spent_cents,
                latency_ms=1,
                success=True,
                error_class=None,
                dispatched_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
    await db.commit()


@pytest.mark.asyncio
async def test_projected_overspend_blocks_under_cap_no_adapter_no_ledger(
    db: AsyncSession, org: Organization, admin_user, credential, default_routing
):
    """Under the cap, but the projected worst-case cost tips it over →
    AICapExceeded, adapter never called, no ledger row for the call.
    """
    # hard=100, spent=97. gpt-4o-mini completion = 60 cents/1M; prompt
    # "hi" adds ~1 token at 15 cents/1M. estimate_cost_cents sums the
    # prompt and completion raw cost, then ceils ONCE: max_tokens=100_000
    # → 6_000_000 + 15 raw = 7 cents (not 6) → 97 + 7 = 104 > 100 →
    # block, even though spent (97) is still strictly under the cap.
    await _seed_cap_and_spend(
        db, org, credential, hard_cap_cents=100, spent_cents=97
    )
    adapter = _make_adapter()
    with patch(
        "app.services.ai_dispatch.get_adapter", return_value=adapter
    ):
        with pytest.raises(AICapExceeded) as exc_info:
            await call_llm(
                db,
                org_id=org.id,
                feature_key="chat",
                request_payload={
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 100_000,
                },
            )
    assert exc_info.value.code == "ai_hard_cap_exceeded"
    adapter.chat.assert_not_awaited()
    rows = (
        await db.execute(
            select(AIUsageLedger).where(AIUsageLedger.org_id == org.id)
        )
    ).scalars().all()
    # Only the seeded prior-spend row; no row for the blocked call.
    assert len(rows) == 1
    assert rows[0].est_cost_cents == 97


@pytest.mark.asyncio
async def test_exhausted_block_still_fires_with_empty_messages(
    db: AsyncSession, org: Organization, admin_user, credential, default_routing
):
    """At/over cap with empty messages (projected == 0): the explicit
    exhausted arm still blocks (regression guard for fail-closed)."""
    await _seed_cap_and_spend(
        db, org, credential, hard_cap_cents=10, spent_cents=12
    )
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
async def test_under_cap_with_headroom_proceeds(
    db: AsyncSession, org: Organization, admin_user, credential, default_routing
):
    """Plenty of headroom → the call proceeds and writes a ledger row."""
    # hard=100_000 cents, no prior spend; projection is ~1 cent.
    await _seed_cap_and_spend(
        db, org, credential, hard_cap_cents=100_000, spent_cents=0
    )
    adapter = _make_adapter(
        LLMResponse(
            content="ok",
            prompt_tokens=5,
            completion_tokens=3,
            model="gpt-4o-mini",
        )
    )
    with patch(
        "app.services.ai_dispatch.get_adapter", return_value=adapter
    ):
        result = await call_llm(
            db,
            org_id=org.id,
            feature_key="chat",
            request_payload={
                "messages": [{"role": "user", "content": "hi"}]
            },
        )
    assert result.response.content == "ok"
    adapter.chat.assert_awaited_once()
    rows = (
        await db.execute(
            select(AIUsageLedger).where(AIUsageLedger.org_id == org.id)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].success is True


@pytest.mark.asyncio
async def test_no_hard_cap_no_gate(
    db: AsyncSession, org: Organization, admin_user, credential, default_routing
):
    """hard_cap_cents is None → no cap configured, no gate, call runs."""
    # No OrgAIDefaultCaps row at all → resolved.hard_cap_cents is None.
    adapter = _make_adapter()
    with patch(
        "app.services.ai_dispatch.get_adapter", return_value=adapter
    ):
        result = await call_llm(
            db,
            org_id=org.id,
            feature_key="chat",
            request_payload={
                "messages": [{"role": "user", "content": "hi"}]
            },
        )
    assert result.response.content == "hi"
    adapter.chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_projection_failure_fails_closed_to_exhausted_only(
    db: AsyncSession, org: Organization, admin_user, credential, default_routing
):
    """If projection raises, the gate degrades to exhausted-only: a
    call with headroom still PROCEEDS (projected pinned to 0), and the
    failure is logged — it must not 500 the hot path."""
    await _seed_cap_and_spend(
        db, org, credential, hard_cap_cents=100, spent_cents=10
    )
    adapter = _make_adapter()
    with patch(
        "app.services.ai_dispatch.get_adapter", return_value=adapter
    ), patch(
        "app.services.ai_dispatch._projected_cost_cents",
        side_effect=RuntimeError("boom"),
    ):
        # spent=10 < hard=100, projected forced to 0 → not blocked.
        result = await call_llm(
            db,
            org_id=org.id,
            feature_key="chat",
            request_payload={
                "messages": [{"role": "user", "content": "hi"}]
            },
        )
    assert result.response.content == "hi"
    adapter.chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_projection_failure_still_blocks_when_exhausted(
    db: AsyncSession, org: Organization, admin_user, credential, default_routing
):
    """Projection raising must not let an already-exhausted org through:
    the explicit exhausted arm fires even with projected pinned to 0."""
    await _seed_cap_and_spend(
        db, org, credential, hard_cap_cents=10, spent_cents=15
    )
    adapter = _make_adapter()
    with patch(
        "app.services.ai_dispatch.get_adapter", return_value=adapter
    ), patch(
        "app.services.ai_dispatch._projected_cost_cents",
        side_effect=RuntimeError("boom"),
    ):
        with pytest.raises(AICapExceeded):
            await call_llm(
                db,
                org_id=org.id,
                feature_key="chat",
                request_payload={
                    "messages": [{"role": "user", "content": "hi"}]
                },
            )
    adapter.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_model_projects_via_default_pricing_and_gates(
    db: AsyncSession, org: Organization, admin_user, credential
):
    """An unknown model still projects (via _default pricing) and gates.

    _default pricing is 1500/6000 cents per 1M. Even a small unpinned
    call projects well over 1 cent, so spent=99 under hard=100 blocks.
    """
    # Route to an unknown model.
    db.add(
        OrgAIDefaultRouting(
            org_id=org.id,
            credential_id=credential.id,
            model="some-unknown-model-v9",
        )
    )
    await _seed_cap_and_spend(
        db, org, credential, hard_cap_cents=100, spent_cents=99
    )
    adapter = _make_adapter()
    with patch(
        "app.services.ai_dispatch.get_adapter", return_value=adapter
    ):
        with pytest.raises(AICapExceeded):
            await call_llm(
                db,
                org_id=org.id,
                feature_key="chat",
                request_payload={
                    "messages": [{"role": "user", "content": "hi"}]
                },
            )
    adapter.chat.assert_not_awaited()


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


# ---------- dispatch timeout (wall-clock bound) ----------------------


@pytest.mark.asyncio
async def test_slow_provider_times_out_cleanly(
    db: AsyncSession,
    org: Organization,
    admin_user,
    credential,
    default_routing,
    monkeypatch,
):
    """A provider call that exceeds the architect-mandated wall-clock
    bound is cancelled cleanly: it maps to a SYSTEM failure
    (``AIDispatchFailed`` code ``provider_timeout``), writes a failure
    ledger row, and surfaces as a 5xx via ``http_for_dispatch_error``.
    The raw provider payload is never echoed.
    """
    import asyncio

    # Shrink the bound so the test stays fast.
    monkeypatch.setattr(app_settings, "ai_dispatch_timeout_s", 0.05)

    async def _slow_chat(*args, **kwargs):
        # Sleeps well past the 0.05s bound; wait_for must cancel it.
        await asyncio.sleep(5)
        return LLMResponse(
            content="never", prompt_tokens=1, completion_tokens=1, model="gpt-4o-mini"
        )

    adapter = MagicMock()
    adapter.chat = _slow_chat

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

    assert exc_info.value.code == "provider_timeout"

    # Maps to a system 5xx (502 bad gateway via the default branch).
    http_exc = ai_dispatch.http_for_dispatch_error(exc_info.value)
    assert http_exc.status_code >= 500
    # No raw provider data echoed — only the typed code.
    assert http_exc.detail == {"code": "provider_timeout"}

    # A failure ledger row is written (system failure, audited).
    rows = (
        await db.execute(select(AIUsageLedger).where(AIUsageLedger.org_id == org.id))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].success is False
    assert rows[0].error_class == "provider_timeout"
    assert rows[0].prompt_tokens == 0
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
            dispatched_at=datetime.now(timezone.utc).replace(tzinfo=None),
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


# ---------- soft cap boundary-crossing (post-write check) -----------


@pytest.mark.asyncio
async def test_soft_cap_crossing_warns_on_boundary_call(
    db: AsyncSession,
    org: Organization,
    admin_user,
    credential,
    default_routing,
    monkeypatch,
):
    """The call that takes usage from below to at-or-above the soft cap
    must trigger the warning, not the NEXT call after the crossing.

    Pre-condition: cost_before=90 cents, soft_cap=100 cents.
    Call cost: 20 cents -> cost_after=110 cents.
    Boundary condition cost_before < soft_cap <= cost_after holds, so
    the post-write check fires exactly once.
    """
    db.add(
        OrgAIDefaultCaps(
            org_id=org.id,
            soft_cap_cents=100,
            hard_cap_cents=None,
            period="monthly",
        )
    )
    # 90 cents of prior usage — below the 100-cent soft cap.
    db.add(
        AIUsageLedger(
            org_id=org.id,
            credential_id=credential.id,
            feature_key="chat",
            model="gpt-4o-mini",
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            est_cost_cents=90,
            latency_ms=1,
            success=True,
            error_class=None,
            dispatched_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
    )
    await db.commit()

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

    # Pin the cost-estimate function so this call costs exactly 20 cents
    # (cost_before=90, cost_after=110, crosses soft_cap=100).
    monkeypatch.setattr(
        "app.services.ai_dispatch.estimate_cost_cents",
        lambda *, model, prompt_tokens, completion_tokens: 20,
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
    # Boundary call dispatched the warning exactly once.
    assert dispatch_mock.await_count == 1
    # Redis marker present after the crossing call.
    assert any(k.startswith("ai_soft_cap_warned:") for k in redis_state)

    # Make a follow-up call costing 5 cents (cost_before=110,
    # cost_after=115, both >= soft_cap). Pre-call check would fire,
    # but the marker is already set so notification is NOT re-dispatched.
    monkeypatch.setattr(
        "app.services.ai_dispatch.estimate_cost_cents",
        lambda *, model, prompt_tokens, completion_tokens: 5,
    )
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


@pytest.mark.asyncio
async def test_soft_cap_not_crossed_no_warn(
    db: AsyncSession,
    org: Organization,
    admin_user,
    credential,
    default_routing,
    monkeypatch,
):
    """When neither the pre-call nor the post-write check matches, no
    warning is dispatched. cost_before=50, cost_after=80, soft_cap=100.
    """
    db.add(
        OrgAIDefaultCaps(
            org_id=org.id,
            soft_cap_cents=100,
            hard_cap_cents=None,
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
            est_cost_cents=50,
            latency_ms=1,
            success=True,
            error_class=None,
            dispatched_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
    )
    await db.commit()

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

    monkeypatch.setattr(
        "app.services.ai_dispatch.estimate_cost_cents",
        lambda *, model, prompt_tokens, completion_tokens: 30,
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
    assert dispatch_mock.await_count == 0
    assert not any(k.startswith("ai_soft_cap_warned:") for k in redis_state)


@pytest.mark.asyncio
async def test_already_above_soft_cap_no_dupe_warn(
    db: AsyncSession,
    org: Organization,
    admin_user,
    credential,
    default_routing,
    monkeypatch,
):
    """If pre-existing usage is already past the soft cap, the pre-call
    check fires once and sets the Redis marker. The post-write check
    on the SAME call must not re-dispatch because the boundary
    condition (cost_before < soft_cap) is false.
    """
    db.add(
        OrgAIDefaultCaps(
            org_id=org.id,
            soft_cap_cents=100,
            hard_cap_cents=None,
            period="monthly",
        )
    )
    # 150 cents of prior usage — already past the 100-cent soft cap.
    db.add(
        AIUsageLedger(
            org_id=org.id,
            credential_id=credential.id,
            feature_key="chat",
            model="gpt-4o-mini",
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            est_cost_cents=150,
            latency_ms=1,
            success=True,
            error_class=None,
            dispatched_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
    )
    await db.commit()

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

    monkeypatch.setattr(
        "app.services.ai_dispatch.estimate_cost_cents",
        lambda *, model, prompt_tokens, completion_tokens: 10,
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
    # Pre-call check fired once. Post-write check skipped because the
    # boundary condition (cost_before < soft_cap) is false.
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
            dispatched_at=datetime.now(timezone.utc).replace(tzinfo=None),
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
            dispatched_at=datetime.now(timezone.utc).replace(tzinfo=None),
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


# ---------- soft-cap marker source-tracking ---------------------------


@pytest.mark.asyncio
async def test_soft_warning_marker_is_default_when_only_hard_cap_is_feature_specific(
    db: AsyncSession,
    org: Organization,
    admin_user,
    credential,
    default_routing,
    monkeypatch,
):
    """Feature row with ONLY a hard cap must NOT scope the soft-cap
    marker to the feature.

    Setup:
      - org_ai_default_caps: soft=100, hard=200
      - org_ai_feature_caps[categorize_transactions]: soft=None, hard=300
      - feature row contributes NOTHING to the effective soft cap
        (300 > 200 default-hard, so default-hard wins for hard; default
        soft=100 wins for soft).

    The soft cap that fires is the org-wide default (100). The Redis
    marker MUST be ``__default__`` so the warning is org-wide and a
    second crossing on a different feature in the same period does
    NOT re-dispatch.
    """
    db.add(
        OrgAIDefaultCaps(
            org_id=org.id,
            soft_cap_cents=100,
            hard_cap_cents=200,
            period="monthly",
        )
    )
    db.add(
        OrgAIFeatureCaps(
            org_id=org.id,
            feature_key="categorize_transactions",
            soft_cap_cents=None,
            hard_cap_cents=300,
            period="monthly",
        )
    )
    # 90 cents prior usage; one 20-cent call crosses the 100-cent default
    # soft cap on the post-write check.
    db.add(
        AIUsageLedger(
            org_id=org.id,
            credential_id=credential.id,
            feature_key="categorize_transactions",
            model="gpt-4o-mini",
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            est_cost_cents=90,
            latency_ms=1,
            success=True,
            error_class=None,
            dispatched_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
    )
    await db.commit()

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

    monkeypatch.setattr(
        "app.services.ai_dispatch.estimate_cost_cents",
        lambda *, model, prompt_tokens, completion_tokens: 20,
    )

    adapter = _make_adapter()
    with patch(
        "app.services.ai_dispatch.get_adapter", return_value=adapter
    ):
        await call_llm(
            db,
            org_id=org.id,
            feature_key="categorize_transactions",
            request_payload={"messages": []},
        )
    # Boundary call dispatched the warning exactly once.
    assert dispatch_mock.await_count == 1
    # The marker must be org-wide (``__default__``), NOT feature-specific.
    period = ai_dispatch._current_period()
    expected_key = f"ai_soft_cap_warned:{org.id}:__default__:{period}"
    assert expected_key in redis_state, (
        f"expected org-wide marker, got keys: {sorted(redis_state)}"
    )
    feature_specific_key = (
        f"ai_soft_cap_warned:{org.id}:categorize_transactions:{period}"
    )
    assert feature_specific_key not in redis_state, (
        "feature-specific marker leaked despite feature row contributing "
        "only a hard cap"
    )

    # Second call against a DIFFERENT feature in the same period that
    # also crosses the same default soft cap MUST NOT re-dispatch.
    # Ledger now totals 110 cents; another 5-cent call keeps it above
    # the 100-cent default soft cap.
    monkeypatch.setattr(
        "app.services.ai_dispatch.estimate_cost_cents",
        lambda *, model, prompt_tokens, completion_tokens: 5,
    )
    with patch(
        "app.services.ai_dispatch.get_adapter", return_value=adapter
    ):
        await call_llm(
            db,
            org_id=org.id,
            feature_key="smart_forecast",
            request_payload={"messages": []},
        )
    # The ``__default__`` marker covers org-wide -> no second dispatch.
    assert dispatch_mock.await_count == 1


@pytest.mark.asyncio
async def test_soft_warning_marker_is_feature_specific_when_feature_has_own_soft_cap(
    db: AsyncSession,
    org: Organization,
    admin_user,
    credential,
    default_routing,
    monkeypatch,
):
    """Feature row with its OWN tighter soft cap must scope the marker
    to that feature.

    Setup:
      - org_ai_default_caps: soft=100, hard=200
      - org_ai_feature_caps[categorize_transactions]: soft=50, hard=None

    A call against categorize_transactions that crosses the 50-cent
    feature soft cap sets a feature-scoped marker. A SAME-feature
    follow-up call does NOT re-dispatch. A DIFFERENT-feature call
    (smart_forecast) that crosses the 100-cent default soft cap fires
    a NEW notification (separate marker scope).
    """
    db.add(
        OrgAIDefaultCaps(
            org_id=org.id,
            soft_cap_cents=100,
            hard_cap_cents=200,
            period="monthly",
        )
    )
    db.add(
        OrgAIFeatureCaps(
            org_id=org.id,
            feature_key="categorize_transactions",
            soft_cap_cents=50,
            hard_cap_cents=None,
            period="monthly",
        )
    )
    # 45 cents prior usage; a 10-cent categorize call crosses
    # feature soft cap (50) on post-write.
    db.add(
        AIUsageLedger(
            org_id=org.id,
            credential_id=credential.id,
            feature_key="categorize_transactions",
            model="gpt-4o-mini",
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            est_cost_cents=45,
            latency_ms=1,
            success=True,
            error_class=None,
            dispatched_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
    )
    await db.commit()

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

    monkeypatch.setattr(
        "app.services.ai_dispatch.estimate_cost_cents",
        lambda *, model, prompt_tokens, completion_tokens: 10,
    )

    adapter = _make_adapter()
    with patch(
        "app.services.ai_dispatch.get_adapter", return_value=adapter
    ):
        await call_llm(
            db,
            org_id=org.id,
            feature_key="categorize_transactions",
            request_payload={"messages": []},
        )
    # First crossing dispatched once with a feature-scoped marker.
    assert dispatch_mock.await_count == 1
    period = ai_dispatch._current_period()
    feature_key_marker = (
        f"ai_soft_cap_warned:{org.id}:categorize_transactions:{period}"
    )
    default_marker = f"ai_soft_cap_warned:{org.id}:__default__:{period}"
    assert feature_key_marker in redis_state, (
        f"expected feature-scoped marker, got keys: {sorted(redis_state)}"
    )
    assert default_marker not in redis_state, (
        "default marker leaked despite feature row supplying its own soft cap"
    )

    # Same-feature follow-up call past the cap -> no dupe notification.
    monkeypatch.setattr(
        "app.services.ai_dispatch.estimate_cost_cents",
        lambda *, model, prompt_tokens, completion_tokens: 5,
    )
    with patch(
        "app.services.ai_dispatch.get_adapter", return_value=adapter
    ):
        await call_llm(
            db,
            org_id=org.id,
            feature_key="categorize_transactions",
            request_payload={"messages": []},
        )
    assert dispatch_mock.await_count == 1

    # Different feature with no feature soft cap. The default soft (100)
    # is now the effective soft cap. Ledger totals 60 cents after the
    # previous calls; we need a smart_forecast call that crosses 100.
    # Use a 50-cent call: cost_before=60, cost_after=110 -> crosses.
    monkeypatch.setattr(
        "app.services.ai_dispatch.estimate_cost_cents",
        lambda *, model, prompt_tokens, completion_tokens: 50,
    )
    with patch(
        "app.services.ai_dispatch.get_adapter", return_value=adapter
    ):
        await call_llm(
            db,
            org_id=org.id,
            feature_key="smart_forecast",
            request_payload={"messages": []},
        )
    # NEW notification fired — different marker scope (__default__).
    assert dispatch_mock.await_count == 2
    assert default_marker in redis_state


# ---------- remaining_hard_cap_cents helper ---------------------------


def _ledger_row(org_id: int, *, cents: int, feature_key: str = "chat"):
    return AIUsageLedger(
        org_id=org_id,
        credential_id=None,
        feature_key=feature_key,
        model="gpt-4o-mini",
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        est_cost_cents=cents,
        latency_ms=0,
        success=True,
        error_class=None,
        dispatched_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )


@pytest.mark.asyncio
async def test_remaining_hard_cap_none_when_no_cap_configured(
    db: AsyncSession, org: Organization
):
    """No hard cap anywhere -> None sentinel (unlimited headroom)."""
    db.add(_ledger_row(org.id, cents=50))
    await db.commit()

    remaining = await ai_dispatch.remaining_hard_cap_cents(
        db, org_id=org.id, feature_key="chat"
    )
    assert remaining is None


@pytest.mark.asyncio
async def test_remaining_hard_cap_subtracts_period_spend(
    db: AsyncSession, org: Organization
):
    """Headroom = hard_cap - cost_so_far for the current month."""
    db.add(
        OrgAIDefaultCaps(
            org_id=org.id,
            soft_cap_cents=None,
            hard_cap_cents=500,
            period="monthly",
        )
    )
    db.add(_ledger_row(org.id, cents=120))
    db.add(_ledger_row(org.id, cents=80))
    await db.commit()

    remaining = await ai_dispatch.remaining_hard_cap_cents(
        db, org_id=org.id, feature_key="chat"
    )
    assert remaining == 300


@pytest.mark.asyncio
async def test_remaining_hard_cap_zero_at_boundary(
    db: AsyncSession, org: Organization
):
    """Spend exactly at the cap -> remaining 0 (dispatch refusal boundary)."""
    db.add(
        OrgAIDefaultCaps(
            org_id=org.id,
            soft_cap_cents=None,
            hard_cap_cents=500,
            period="monthly",
        )
    )
    db.add(_ledger_row(org.id, cents=500))
    await db.commit()

    remaining = await ai_dispatch.remaining_hard_cap_cents(
        db, org_id=org.id, feature_key="chat"
    )
    assert remaining == 0


@pytest.mark.asyncio
async def test_remaining_hard_cap_negative_when_over(
    db: AsyncSession, org: Organization
):
    """Over the cap -> negative headroom (no clamping)."""
    db.add(
        OrgAIDefaultCaps(
            org_id=org.id,
            soft_cap_cents=None,
            hard_cap_cents=100,
            period="monthly",
        )
    )
    db.add(_ledger_row(org.id, cents=180))
    await db.commit()

    remaining = await ai_dispatch.remaining_hard_cap_cents(
        db, org_id=org.id, feature_key="chat"
    )
    assert remaining == -80


@pytest.mark.asyncio
async def test_remaining_hard_cap_feature_cap_tighter_wins(
    db: AsyncSession, org: Organization
):
    """Tighter feature hard cap wins over the default, same as dispatch."""
    db.add(
        OrgAIDefaultCaps(
            org_id=org.id,
            soft_cap_cents=None,
            hard_cap_cents=1000,
            period="monthly",
        )
    )
    db.add(
        OrgAIFeatureCaps(
            org_id=org.id,
            feature_key="chat",
            soft_cap_cents=None,
            hard_cap_cents=200,
            period="monthly",
        )
    )
    db.add(_ledger_row(org.id, cents=50))
    await db.commit()

    remaining = await ai_dispatch.remaining_hard_cap_cents(
        db, org_id=org.id, feature_key="chat"
    )
    # 200 (feature wins) - 50 spent = 150
    assert remaining == 150
