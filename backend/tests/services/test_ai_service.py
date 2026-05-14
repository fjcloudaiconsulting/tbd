"""LAI Foundation — PR-B tests for ``app.services.ai_service``.

Spec: ``~/.claude/projects/-Users-fjorge-src-pfv/specs/2026-05-14-lai-foundation.md``

PR-B is mock-only. The cap-check is a stub (always within budget) and
the real-provider adapters are not wired. These tests pin:

- Feature-gate fail-closed: a closed gate raises ``FeatureNotEnabled``
  and emits a ``rejected_gate_closed`` structlog event.
- Dry-run path: ``dry_run=True`` returns the deterministic mock content
  with ``cost_cents=0``.
- Mock adapter idempotency: the same ``Prompt`` returns the same
  ``content`` across two calls.
- Prompt redaction contract: ``redaction_certified is not True`` raises
  ``PromptNotRedacted``.
- PII key rejection: ``user_context`` with an IBAN-shaped key raises
  ``PromptContainsPII``.
- Structlog privacy: the ``ai.call`` event never contains prompt
  content under any key.

Test-pattern note: structlog routes through its own renderer, so
``caplog`` does not see records. We patch ``ai_service.logger.ainfo``
with ``AsyncMock`` to capture event payloads (same pattern as
``backend/tests/services/test_import_preview_with_rules.py`` for the
``smart_rules.preview_built`` event).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.subscription import (
    BillingInterval,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.models.user import Organization
from app.services import ai_service
from app.services.ai_service import (
    FeatureNotEnabled,
    LLMResult,
    Prompt,
    PromptContainsPII,
    PromptNotRedacted,
    STATUS_DRY_RUN,
    STATUS_REJECTED_GATE_CLOSED,
    STATUS_SUCCESS,
    call_llm,
)
from app.services.exceptions import ValidationError


# ---------------------------------------------------------------------------
# Fixtures — same pattern as test_feature_service.py
# ---------------------------------------------------------------------------


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


async def _make_org(db: AsyncSession, *, name: str = "Acme") -> Organization:
    org = Organization(name=name, billing_cycle_day=1)
    db.add(org)
    await db.commit()
    return org


async def _make_plan(db: AsyncSession, *, features: dict[str, bool]) -> Plan:
    plan = Plan(slug="pro", name="Pro", features=features)
    db.add(plan)
    await db.commit()
    return plan


async def _make_sub(db: AsyncSession, *, org_id: int, plan_id: int) -> Subscription:
    sub = Subscription(
        org_id=org_id,
        plan_id=plan_id,
        status=SubscriptionStatus.ACTIVE,
        billing_interval=BillingInterval.MONTHLY,
    )
    db.add(sub)
    await db.commit()
    return sub


_ALL_FALSE = {
    "ai.budget": False,
    "ai.forecast": False,
    "ai.smart_plan": False,
    "ai.autocategorize": False,
}


def _enabled_features(*keys: str) -> dict[str, bool]:
    out = dict(_ALL_FALSE)
    for k in keys:
        out[k] = True
    return out


@pytest.fixture
def redacted_prompt() -> Prompt:
    return Prompt(
        system_instructions="Categorize this merchant.",
        user_context={"normalized_token": "JUMBO"},
        redaction_certified=True,
    )


def _capture_ai_calls(spy: AsyncMock) -> list[dict[str, Any]]:
    """Return kwargs for every ``ai.call`` structlog event the spy saw."""
    return [
        c.kwargs
        for c in spy.call_args_list
        if c.args and c.args[0] == "ai.call"
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feature_gate_fail_closed(session_factory, redacted_prompt):
    """Org without ``ai.autocategorize`` -> call_llm raises
    FeatureNotEnabled and does NOT reach the mock adapter."""
    async with session_factory() as db:
        org = await _make_org(db)
        plan = await _make_plan(db, features=_ALL_FALSE)
        await _make_sub(db, org_id=org.id, plan_id=plan.id)

        with pytest.raises(FeatureNotEnabled) as exc_info:
            await call_llm(
                db,
                org_id=org.id,
                feature_key="ai.autocategorize",
                prompt=redacted_prompt,
            )
        assert exc_info.value.org_id == org.id
        assert exc_info.value.feature_key == "ai.autocategorize"


@pytest.mark.asyncio
async def test_dry_run_returns_canned_mock(session_factory, redacted_prompt):
    """``dry_run=True`` with an enabled gate returns a deterministic
    mock result with ``dry_run=True`` and ``cost_cents=0``."""
    async with session_factory() as db:
        org = await _make_org(db)
        plan = await _make_plan(db, features=_enabled_features("ai.autocategorize"))
        await _make_sub(db, org_id=org.id, plan_id=plan.id)

        result = await call_llm(
            db,
            org_id=org.id,
            feature_key="ai.autocategorize",
            prompt=redacted_prompt,
            dry_run=True,
        )

    assert isinstance(result, LLMResult)
    assert result.dry_run is True
    assert result.cost_cents == 0
    assert result.tokens_in == 0
    assert result.tokens_out == 0
    assert result.provider == "mock"
    # Mock adapter dispatch always reports ``dry_run`` status, even when
    # the caller did not explicitly request dry-run — every mock call is
    # by definition a non-billable, non-network operation.
    assert result.status == STATUS_DRY_RUN
    assert result.content.startswith("[mock:")
    assert result.request_id  # service-generated UUID hex


@pytest.mark.asyncio
async def test_mock_adapter_idempotency(session_factory, redacted_prompt):
    """Same Prompt across two ``call_llm`` invocations returns the same
    ``content`` (mock adapter is a pure function of the prompt)."""
    async with session_factory() as db:
        org = await _make_org(db)
        plan = await _make_plan(db, features=_enabled_features("ai.autocategorize"))
        await _make_sub(db, org_id=org.id, plan_id=plan.id)

        r1 = await call_llm(
            db,
            org_id=org.id,
            feature_key="ai.autocategorize",
            prompt=redacted_prompt,
        )
        r2 = await call_llm(
            db,
            org_id=org.id,
            feature_key="ai.autocategorize",
            prompt=redacted_prompt,
        )

    assert r1.content == r2.content
    # request_id differs per call so feedback signals (PR-D) can be
    # correlated back to a specific invocation.
    assert r1.request_id != r2.request_id


@pytest.mark.asyncio
async def test_prompt_redaction_certified_required(session_factory):
    """Prompt without ``redaction_certified=True`` raises
    PromptNotRedacted before any gate check or adapter dispatch."""
    async with session_factory() as db:
        org = await _make_org(db)
        plan = await _make_plan(db, features=_enabled_features("ai.autocategorize"))
        await _make_sub(db, org_id=org.id, plan_id=plan.id)

        unredacted = Prompt(
            system_instructions="x",
            user_context={"normalized_token": "X"},
            # redaction_certified defaults to False
        )

        with pytest.raises(PromptNotRedacted):
            await call_llm(
                db,
                org_id=org.id,
                feature_key="ai.autocategorize",
                prompt=unredacted,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_key",
    ["iban", "ACCOUNT_NUMBER", "FullName", "ssn", "tax_id"],
)
async def test_pii_key_rejection(session_factory, bad_key):
    """``user_context`` with a PII-shaped key (IBAN, account_number,
    full_name, ssn, tax_id) raises PromptContainsPII."""
    async with session_factory() as db:
        org = await _make_org(db)
        plan = await _make_plan(db, features=_enabled_features("ai.autocategorize"))
        await _make_sub(db, org_id=org.id, plan_id=plan.id)

        bad_prompt = Prompt(
            system_instructions="x",
            user_context={bad_key: "REDACTED-LOOKING-VALUE"},
            redaction_certified=True,
        )
        with pytest.raises(PromptContainsPII) as exc_info:
            await call_llm(
                db,
                org_id=org.id,
                feature_key="ai.autocategorize",
                prompt=bad_prompt,
            )
        assert exc_info.value.key == bad_key


@pytest.mark.asyncio
async def test_unknown_feature_key_raises_validation_error(
    session_factory, redacted_prompt
):
    """A feature_key not in ALL_FEATURE_KEYS raises ValidationError."""
    async with session_factory() as db:
        org = await _make_org(db)

        with pytest.raises(ValidationError):
            await call_llm(
                db,
                org_id=org.id,
                feature_key="ai.does_not_exist",  # type: ignore[arg-type]
                prompt=redacted_prompt,
            )


@pytest.mark.asyncio
async def test_structlog_event_contains_no_prompt_content(
    session_factory, redacted_prompt
):
    """The ``ai.call`` structlog event must never contain prompt
    content under any key. Spec §6 / §8 privacy invariant."""
    secret_marker = "SECRET_PROMPT_BODY_DO_NOT_LOG"
    prompt = Prompt(
        system_instructions=secret_marker,
        user_context={"normalized_token": secret_marker},
        redaction_certified=True,
    )

    async with session_factory() as db:
        org = await _make_org(db)
        plan = await _make_plan(db, features=_enabled_features("ai.autocategorize"))
        await _make_sub(db, org_id=org.id, plan_id=plan.id)

        with patch.object(
            ai_service.logger, "ainfo", new_callable=AsyncMock
        ) as spy:
            result = await call_llm(
                db,
                org_id=org.id,
                feature_key="ai.autocategorize",
                prompt=prompt,
            )

    # Result content must not echo the secret marker.
    assert secret_marker not in result.content

    # Every ``ai.call`` event's kwargs (and the positional event name
    # itself) must be marker-free.
    events = _capture_ai_calls(spy)
    assert len(events) == 1, f"expected exactly one ai.call event, got {len(events)}"
    rendered = repr(events[0])
    assert secret_marker not in rendered, (
        f"Prompt content leaked into ai.call event kwargs: {events[0]}"
    )

    # Telemetry whitelist (spec §6) — must contain the field set, NOT
    # any prompt/completion content.
    expected_keys = {
        "org_id", "feature_key", "provider", "model",
        "tokens_in", "tokens_out", "cost_cents", "latency_ms",
        "dry_run", "status", "error_code", "request_id",
    }
    assert set(events[0].keys()) == expected_keys


@pytest.mark.asyncio
async def test_gate_closed_emits_structlog_event(session_factory, redacted_prompt):
    """A gate-closed rejection still emits a structlog ``ai.call`` event
    with ``status=rejected_gate_closed`` so the coverage endpoint can
    count rejections as a distinct outcome."""
    async with session_factory() as db:
        org = await _make_org(db)
        plan = await _make_plan(db, features=_ALL_FALSE)
        await _make_sub(db, org_id=org.id, plan_id=plan.id)

        with patch.object(
            ai_service.logger, "ainfo", new_callable=AsyncMock
        ) as spy:
            with pytest.raises(FeatureNotEnabled):
                await call_llm(
                    db,
                    org_id=org.id,
                    feature_key="ai.autocategorize",
                    prompt=redacted_prompt,
                )

    events = _capture_ai_calls(spy)
    assert len(events) == 1
    assert events[0]["status"] == STATUS_REJECTED_GATE_CLOSED
    assert events[0]["org_id"] == org.id
    assert events[0]["feature_key"] == "ai.autocategorize"
    # No tokens charged, no provider dispatched.
    assert events[0]["tokens_in"] == 0
    assert events[0]["tokens_out"] == 0
    assert events[0]["cost_cents"] == 0
