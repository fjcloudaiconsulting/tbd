"""Tests for Task 4 of ai-forecast-refine-cost-confirmed:
- refine_forecast passes a sized max_tokens to call_llm_structured
- estimate_refine returns token estimates without dispatching to the LLM
"""
from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
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
from app.models.ai_usage_ledger import AIUsageLedger
from app.models.org_ai_caps import OrgAIDefaultCaps
from app.models.org_ai_credential import AiProvider, OrgAICredential
from app.models.org_ai_routing import OrgAIDefaultRouting
from app.models.user import Organization, Role, User
from app.security import hash_password
from app.services import ai_dispatch, ai_forecast_refine_service as svc
from app.services.ai_credential_crypto import encrypt
from app.services.ai_forecast_refine_token_estimate import Scope


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
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(session_factory) -> AsyncIterator[AsyncSession]:
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
async def seeded_org(db_session: AsyncSession) -> Organization:
    """Minimal org with a user + routing so the service can resolve a model."""
    org = Organization(name="TestOrg", billing_cycle_day=1)
    db_session.add(org)
    await db_session.commit()

    user = User(
        org_id=org.id,
        username="owner",
        email="owner@example.com",
        password_hash=hash_password("pw-testtest"),
        role=Role.OWNER,
        is_superadmin=False,
        is_active=True,
        email_verified=True,
    )
    db_session.add(user)
    await db_session.commit()

    cred = OrgAICredential(
        org_id=org.id,
        provider=AiProvider.OPENAI,
        encrypted_api_key=encrypt("sk-test-12345"),
        encrypted_bearer_token=None,
        base_url=None,
        key_fingerprint="0123456789abcdef",
        last_four="2345",
        label="primary",
        discovered_capabilities=["chat", "structured_output"],
    )
    db_session.add(cred)
    await db_session.commit()

    routing = OrgAIDefaultRouting(
        org_id=org.id, credential_id=cred.id, model="gpt-4o-mini"
    )
    db_session.add(routing)
    await db_session.commit()

    return org


# ---------- tests ------------------------------------------------------


_FAKE_BASELINE = {
    "period_start": "2026-06-01",
    "period_end": "2026-06-30",
    "forecast_income": "5000",
    "forecast_expense": "3000",
    # TBD-325 PR 2. ⚠ REQUIRED, not decoration. ``compute_forecast`` always
    # returns this key and ``_build_category_history`` reads it off the
    # baseline, so a fake without it exercises an arm the real producer cannot
    # produce -- which is how the fail-open ``.get()`` this replaced stayed
    # invisible in a green suite.
    "currency_scope": {
        "currency": None,
        "excluded_currencies": [],
        "excluded_account_count": 0,
    },
    "categories": [
        {"category_id": 1, "category_name": "Rent", "forecast": "1500"},
        {"category_id": 2, "category_name": "Food", "forecast": "600"},
    ],
}

_FAKE_HISTORY = [
    {"category_id": 1, "month": "2026-05", "total_expense": "1500"},
    {"category_id": 2, "month": "2026-05", "total_expense": "600"},
]


@pytest.mark.asyncio
async def test_refine_passes_sized_max_tokens(
    monkeypatch, db_session: AsyncSession, seeded_org: Organization
):
    """refine_forecast must pass a sized max_tokens (>= 1024) to
    call_llm_structured even when NoRoutingConfigured is raised (the
    fallback path). Verifies that max_tokens is computed and threaded
    through before the dispatch attempt.
    """
    captured: dict = {}

    # Patch internals so we can reach the dispatch call even in an empty DB
    async def fake_compute_forecast(db, org_id, period_start=None):
        return _FAKE_BASELINE

    async def fake_build_history(
        db, *, org_id, period_start, months=12, currency_scope=None
    ):
        return _FAKE_HISTORY

    async def fake_category_index(db, *, org_id):
        return {1: "Rent", 2: "Food"}

    async def fake_structured(
        db, *, org_id, feature_key, messages, response_schema, max_tokens=None
    ):
        captured["max_tokens"] = max_tokens
        raise ai_dispatch.NoRoutingConfigured()  # force clean fallback

    monkeypatch.setattr(svc.forecast_service, "compute_forecast", fake_compute_forecast)
    monkeypatch.setattr(svc, "_build_category_history", fake_build_history)
    monkeypatch.setattr(svc, "_category_index", fake_category_index)
    monkeypatch.setattr(ai_dispatch, "call_llm_structured", fake_structured)

    resp = await svc.refine_forecast(
        db_session,
        org_id=seeded_org.id,
        scope=Scope.TOP_20,
        timeframe_months=6,
    )
    assert resp.provenance.ai_applied is False
    assert captured.get("max_tokens") is not None
    assert captured["max_tokens"] >= 1024


@pytest.mark.asyncio
async def test_estimate_refine_returns_tokens_without_dispatch(
    monkeypatch, db_session: AsyncSession, seeded_org: Organization
):
    """estimate_refine must compute positive token estimates and return them
    without ever calling call_llm_structured (even with routing + history).
    """
    called: dict = {"dispatch": False}

    async def fake_compute_forecast(db, org_id, period_start=None):
        return _FAKE_BASELINE

    async def fake_build_history(
        db, *, org_id, period_start, months=12, currency_scope=None
    ):
        return _FAKE_HISTORY

    async def fake_category_index(db, *, org_id):
        return {1: "Rent", 2: "Food"}

    async def fake_structured(*a, **k):
        called["dispatch"] = True

    monkeypatch.setattr(svc.forecast_service, "compute_forecast", fake_compute_forecast)
    monkeypatch.setattr(svc, "_build_category_history", fake_build_history)
    monkeypatch.setattr(svc, "_category_index", fake_category_index)
    monkeypatch.setattr(ai_dispatch, "call_llm_structured", fake_structured)

    est = await svc.estimate_refine(
        db_session,
        org_id=seeded_org.id,
        period_start=None,
        timeframe_months=6,
        scope=Scope.TOP_20,
    )
    assert called["dispatch"] is False
    assert est.est_prompt_tokens > 0
    assert est.est_output_tokens > 0


@pytest.mark.asyncio
async def test_estimate_refine_no_history_returns_insufficient_history(
    monkeypatch, db_session: AsyncSession, seeded_org: Organization
):
    """When the org has no history at all, estimate_refine returns
    can_proceed=False with reason='insufficient_history'.
    """
    called: dict = {"dispatch": False}

    async def fake_structured(*a, **k):
        called["dispatch"] = True

    monkeypatch.setattr(ai_dispatch, "call_llm_structured", fake_structured)

    est = await svc.estimate_refine(
        db_session,
        org_id=seeded_org.id,
        period_start=None,
        timeframe_months=6,
        scope=Scope.TOP_20,
    )
    assert called["dispatch"] is False
    assert est.can_proceed is False
    assert est.reason == "insufficient_history"


# ---------- spend-cap pre-check on estimate ---------------------------
#
# These mirror the hard-cap enforcement in ai_dispatch.call_llm_structured
# (cost_so_far >= hard_cap -> refuse) so the /estimate preflight and the
# real dispatch agree on whether a refine can run. The estimate must also
# refuse when the projected cost would push the org over the remaining
# budget, so the UI never offers Confirm when Confirm would hard-fail.


def _patch_estimate_internals(monkeypatch, *, est_cost_cents: int):
    """Stub the no-DB estimate inputs so the cap pre-check is the only
    variable under test. compute_forecast / history / index are faked, and
    estimate_cost_cents is pinned to a deterministic projected cost.
    """
    async def fake_compute_forecast(db, org_id, period_start=None):
        return _FAKE_BASELINE

    async def fake_build_history(
        db, *, org_id, period_start, months=12, currency_scope=None
    ):
        return _FAKE_HISTORY

    async def fake_category_index(db, *, org_id):
        return {1: "Rent", 2: "Food"}

    monkeypatch.setattr(svc.forecast_service, "compute_forecast", fake_compute_forecast)
    monkeypatch.setattr(svc, "_build_category_history", fake_build_history)
    monkeypatch.setattr(svc, "_category_index", fake_category_index)
    monkeypatch.setattr(svc, "estimate_cost_cents", lambda **kw: est_cost_cents)


async def _seed_cap_and_spend(
    db_session: AsyncSession,
    *,
    org_id: int,
    hard_cap_cents: int,
    spent_cents: int,
):
    """Create an org hard cap and a settled ledger row of ``spent_cents``."""
    db_session.add(
        OrgAIDefaultCaps(
            org_id=org_id, soft_cap_cents=None, hard_cap_cents=hard_cap_cents
        )
    )
    if spent_cents > 0:
        db_session.add(
            AIUsageLedger(
                org_id=org_id,
                credential_id=None,
                feature_key=svc.ROUTING_KEY,
                model="gpt-4o-mini",
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                est_cost_cents=spent_cents,
                latency_ms=0,
                success=True,
            )
        )
    await db_session.commit()


@pytest.mark.asyncio
async def test_estimate_refine_at_hard_cap_refuses(
    monkeypatch, db_session: AsyncSession, seeded_org: Organization
):
    """Org sitting exactly AT its hard cap must get can_proceed=False, even
    if the projected cost is tiny. Boundary mirrors dispatch's
    ``cost_so_far >= hard_cap`` (remaining == 0).
    """
    _patch_estimate_internals(monkeypatch, est_cost_cents=1)
    await _seed_cap_and_spend(
        db_session, org_id=seeded_org.id, hard_cap_cents=500, spent_cents=500
    )

    est = await svc.estimate_refine(
        db_session,
        org_id=seeded_org.id,
        period_start=None,
        timeframe_months=6,
        scope=Scope.TOP_20,
    )
    assert est.can_proceed is False
    assert est.reason == "ai_cap_exceeded"


@pytest.mark.asyncio
async def test_estimate_refine_projected_cost_over_remaining_refuses(
    monkeypatch, db_session: AsyncSession, seeded_org: Organization
):
    """Org has headroom, but the projected cost is larger than what's left
    before the hard cap. Confirm would hard-fail, so the estimate refuses.

    remaining = 500 - 450 = 50; projected = 80 > 50 -> refuse.
    """
    _patch_estimate_internals(monkeypatch, est_cost_cents=80)
    await _seed_cap_and_spend(
        db_session, org_id=seeded_org.id, hard_cap_cents=500, spent_cents=450
    )

    est = await svc.estimate_refine(
        db_session,
        org_id=seeded_org.id,
        period_start=None,
        timeframe_months=6,
        scope=Scope.TOP_20,
    )
    assert est.can_proceed is False
    assert est.reason == "ai_cap_exceeded"


@pytest.mark.asyncio
async def test_estimate_refine_ample_budget_allows(
    monkeypatch, db_session: AsyncSession, seeded_org: Organization
):
    """Org with plenty of remaining budget can proceed (regression).

    remaining = 5000 - 100 = 4900; projected = 80 << 4900 -> allow.
    """
    _patch_estimate_internals(monkeypatch, est_cost_cents=80)
    await _seed_cap_and_spend(
        db_session, org_id=seeded_org.id, hard_cap_cents=5000, spent_cents=100
    )

    est = await svc.estimate_refine(
        db_session,
        org_id=seeded_org.id,
        period_start=None,
        timeframe_months=6,
        scope=Scope.TOP_20,
    )
    assert est.can_proceed is True
    assert est.reason is None
    assert est.est_cost_cents == 80


# ---------- TBD-261: refuse a non-monthly baseline window --------------
#
# Refinement's system prompt tells the LLM it is looking at a "baseline
# monthly forecast" and applies dimensionless multipliers on top of it.
# A lapsed open period (floored at today) or a manually closed long
# period can span many months; applying monthly seasonality multipliers
# to that total is meaningless. The guard must fire deterministically,
# before any history build or LLM dispatch.

_LONG_BASELINE = {
    **_FAKE_BASELINE,
    "period_start": "2026-03-01",
    "period_end": "2026-05-30",  # 91 inclusive days
}


def _make_baseline(period_start: str, period_end: str) -> dict:
    return {**_FAKE_BASELINE, "period_start": period_start, "period_end": period_end}


@pytest.mark.asyncio
async def test_refine_refuses_non_monthly_window(
    monkeypatch, db_session: AsyncSession, seeded_org: Organization
):
    """F1: a 91-day baseline window refuses before touching history or
    dispatch, and the refined response equals the baseline.
    """
    history_calls = {"n": 0}
    dispatch_calls = {"n": 0}

    async def fake_compute_forecast(db, org_id, period_start=None):
        return _LONG_BASELINE

    async def fake_build_history(*a, **k):
        history_calls["n"] += 1
        return _FAKE_HISTORY

    async def fake_structured(*a, **k):
        dispatch_calls["n"] += 1
        raise AssertionError("dispatch must not be called")

    monkeypatch.setattr(svc.forecast_service, "compute_forecast", fake_compute_forecast)
    monkeypatch.setattr(svc, "_build_category_history", fake_build_history)
    monkeypatch.setattr(ai_dispatch, "call_llm_structured", fake_structured)

    resp = await svc.refine_forecast(
        db_session,
        org_id=seeded_org.id,
        scope=Scope.TOP_20,
        timeframe_months=6,
    )

    assert resp.provenance.ai_applied is False
    assert resp.provenance.fallback_reason == "forecast_window_not_monthly"
    assert resp.refined_forecast_expense == resp.baseline_forecast_expense
    assert resp.refined_forecast_income == resp.baseline_forecast_income
    assert history_calls["n"] == 0
    assert dispatch_calls["n"] == 0


@pytest.mark.asyncio
async def test_estimate_refuses_non_monthly_window(
    monkeypatch, db_session: AsyncSession, seeded_org: Organization
):
    """F2: estimate_refine on the same 91-day baseline refuses too, proving
    the guard isn't wired into only one of the two entry points.
    """
    async def fake_compute_forecast(db, org_id, period_start=None):
        return _LONG_BASELINE

    async def fake_build_history(*a, **k):
        raise AssertionError("history must not be built")

    async def fake_structured(*a, **k):
        raise AssertionError("dispatch must not be called")

    monkeypatch.setattr(svc.forecast_service, "compute_forecast", fake_compute_forecast)
    monkeypatch.setattr(svc, "_build_category_history", fake_build_history)
    monkeypatch.setattr(ai_dispatch, "call_llm_structured", fake_structured)

    est = await svc.estimate_refine(
        db_session,
        org_id=seeded_org.id,
        period_start=None,
        timeframe_months=6,
        scope=Scope.TOP_20,
    )

    assert est.can_proceed is False
    assert est.reason == "forecast_window_not_monthly"


@pytest.mark.asyncio
async def test_refine_window_boundary_35_dispatches_36_refuses(
    monkeypatch, db_session: AsyncSession, seeded_org: Organization
):
    """F3: a 35-day (inclusive) window is still monthly-ish and dispatches;
    36 days refuses. Kills exclusive day-count and off-by-one mutants.
    """
    from app.schemas.ai_forecast import AIForecastAdjustments
    from app.services.ai_providers.base import StructuredResponse

    baselines = {
        35: _make_baseline("2026-01-01", "2026-02-04"),
        36: _make_baseline("2026-01-01", "2026-02-05"),
    }
    results: dict[int, bool] = {}

    for days, baseline in baselines.items():
        dispatch_calls = {"n": 0}

        async def fake_compute_forecast(db, org_id, period_start=None, _b=baseline):
            return _b

        async def fake_build_history(*a, **k):
            return _FAKE_HISTORY

        async def fake_category_index(db, *, org_id):
            return {1: "Rent", 2: "Food"}

        async def fake_structured(
            db, *, org_id, feature_key, messages, response_schema, max_tokens=None,
            _dc=dispatch_calls,
        ):
            _dc["n"] += 1
            parsed = AIForecastAdjustments(
                seasonal=[], anomalies=[], confidence=0.5, summary="ok"
            ).model_dump()
            return svc.ai_dispatch.StructuredDispatchResult(
                response=StructuredResponse(
                    parsed=parsed,
                    raw_text="{}",
                    prompt_tokens=10,
                    completion_tokens=10,
                    model="gpt-4o-mini",
                    retries_used=0,
                ),
                ledger_id=1,
            )

        monkeypatch.setattr(
            svc.forecast_service, "compute_forecast", fake_compute_forecast
        )
        monkeypatch.setattr(svc, "_build_category_history", fake_build_history)
        monkeypatch.setattr(svc, "_category_index", fake_category_index)
        monkeypatch.setattr(ai_dispatch, "call_llm_structured", fake_structured)

        resp = await svc.refine_forecast(
            db_session,
            org_id=seeded_org.id,
            scope=Scope.TOP_20,
            timeframe_months=6,
        )
        results[days] = dispatch_calls["n"] > 0
        if days == 36:
            assert resp.provenance.fallback_reason == "forecast_window_not_monthly"

    assert results[35] is True
    assert results[36] is False


@pytest.mark.asyncio
async def test_refine_35_day_prompt_payload_is_unmodified(
    monkeypatch, db_session: AsyncSession, seeded_org: Organization
):
    """F4: in the 35-day (dispatching) case, the prompt payload's
    baseline_forecast period_start/period_end and forecast_expense match
    the baseline exactly, and the system prompt still says "baseline
    monthly forecast". Kills silent re-windowing/normalising of the
    payload.
    """
    from app.schemas.ai_forecast import AIForecastAdjustments
    from app.services.ai_providers.base import StructuredResponse

    baseline = _make_baseline("2026-01-01", "2026-02-04")
    captured: dict = {}

    async def fake_compute_forecast(db, org_id, period_start=None):
        return baseline

    async def fake_build_history(*a, **k):
        return _FAKE_HISTORY

    async def fake_category_index(db, *, org_id):
        return {1: "Rent", 2: "Food"}

    async def fake_structured(
        db, *, org_id, feature_key, messages, response_schema, max_tokens=None
    ):
        captured["messages"] = messages
        parsed = AIForecastAdjustments(
            seasonal=[], anomalies=[], confidence=0.5, summary="ok"
        ).model_dump()
        return svc.ai_dispatch.StructuredDispatchResult(
            response=StructuredResponse(
                parsed=parsed,
                raw_text="{}",
                prompt_tokens=10,
                completion_tokens=10,
                model="gpt-4o-mini",
                retries_used=0,
            ),
            ledger_id=1,
        )

    monkeypatch.setattr(svc.forecast_service, "compute_forecast", fake_compute_forecast)
    monkeypatch.setattr(svc, "_build_category_history", fake_build_history)
    monkeypatch.setattr(svc, "_category_index", fake_category_index)
    monkeypatch.setattr(ai_dispatch, "call_llm_structured", fake_structured)

    await svc.refine_forecast(
        db_session,
        org_id=seeded_org.id,
        scope=Scope.TOP_20,
        timeframe_months=6,
    )

    messages = captured["messages"]
    system_msg = next(m for m in messages if m["role"] == "system")
    user_msg = next(m for m in messages if m["role"] == "user")
    payload = __import__("json").loads(user_msg["content"])

    assert payload["baseline_forecast"]["period_start"] == baseline["period_start"]
    assert payload["baseline_forecast"]["period_end"] == baseline["period_end"]
    assert payload["baseline_forecast"]["forecast_expense"] == baseline["forecast_expense"]
    assert "baseline monthly forecast" in system_msg["content"]
