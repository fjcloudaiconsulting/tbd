"""Service tests for the AI-enhanced scenario engine wrapper (PR4).

Architect-locked invariants pinned here:

- The wrapper engages ONLY when the ``ai.smart_plan`` feature gate is
  open AND ``smart_plan`` routing is configured. Otherwise the
  analytic baseline is returned unchanged.
- LLM responses are parsed via a strict Pydantic schema. Schema
  mismatch -> fall back to analytic, never propagate.
- Any LLM transport failure (StructuredOutputError, AIDispatchFailed,
  NoRoutingConfigured) -> fall back to analytic. The frontend never
  crashes because AI couldn't deliver.
- On every AI invocation (success OR fallback), an audit event is
  emitted via ``audit_service.record_audit_event``.
- The wrapper still respects the sandboxing guarantee: it only reads
  the world state; the analytic engine math does the projection.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

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

from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.scenario import Scenario, ScenarioType
from app.models.user import Organization, Role, User
from app.security import hash_password
from app.services.ai_dispatch import (
    AIDispatchFailed,
    NoRoutingConfigured,
    StructuredDispatchResult,
)
from app.services.ai_providers.base import StructuredOutputError, StructuredResponse
from app.services.scenario_engine import (
    AccountSnapshot,
    AnalyticEngine,
    SimulationRequest,
    WorldState,
)
from app.services.scenario_engine_ai import (
    AI_ASSUMPTION_SCHEMA,
    AIAssumptionDelta,
    run_ai_simulation,
)


# ── fixtures ────────────────────────────────────────────────────────────


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
async def db(session_factory):
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def seed_org_user(session_factory) -> dict:
    """Seed minimal org + user so audit-event FK constraints pass.

    The audit_events table FKs to organizations(id) and users(id); without
    these rows the SQLite ON-FK-violation aborts the audit row write and
    structurally invalidates the "did the wrapper audit?" assertion.
    """
    async with session_factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        user = User(
            org_id=org.id,
            username="actor",
            email="actor@example.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            is_superadmin=False,
            is_active=True,
            email_verified=True,
        )
        db.add(user)
        await db.commit()
        return {"org_id": org.id, "user_id": user.id, "email": user.email}


def _make_retirement_scenario(
    *, horizon_months: int = 360, org_id: int = 1, user_id: int = 1
) -> Scenario:
    """Minimal retirement scenario the wrapper can run against."""
    return Scenario(
        org_id=org_id,
        user_id=user_id,
        name="Retirement",
        scenario_type=ScenarioType.RETIREMENT,
        params_json={
            "scenario_type": "retirement",
            "target_retirement_date": (
                date.today().replace(day=1) + timedelta(days=horizon_months * 30)
            ).isoformat(),
            "currency": "EUR",
            "monthly_contribution": "500.00",
            "contribution_account_id": 1,
            "target_balance": "100000.00",
            "annual_return_pct": "6.0",
            "inflation_pct": "2.5",
            "contribution_curve": [],
        },
        horizon_months=horizon_months,
    )


def _make_world_state() -> WorldState:
    return WorldState(
        accounts=[
            AccountSnapshot(
                account_id=1,
                account_name="Retirement Fund",
                currency="EUR",
                starting_balance=Decimal("0"),
            )
        ],
        recurring=[],
        history=[],
    )


def _make_request(scenario: Scenario) -> SimulationRequest:
    return SimulationRequest(
        scenario=scenario,
        state=_make_world_state(),
        horizon_months=scenario.horizon_months,
        options={},
    )


# Audit context passed at every ``run_ai_simulation`` call site. Real
# values (not None) so the audit tests can assert they reach the row —
# a NULL-vs-wrong-value bug is invisible if the tests only pass None.
_REQUEST_ID = "rid-abc123"
_IP_ADDRESS = "203.0.113.9"


# ── helpers ─────────────────────────────────────────────────────────────


async def _count_audit_rows(session_factory, event_type: str) -> int:
    from sqlalchemy import func, select
    async with session_factory() as db:
        return (
            await db.execute(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == event_type)
            )
        ).scalar_one()


async def _assert_audit_context(session_factory, event_type: str) -> None:
    """Assert the caller's audit context reached the persisted row.

    Guards the failure mode a required-kwarg signature cannot: a caller
    passing a WRONG value (e.g. the raw proxy peer instead of the
    resolved client IP) rather than omitting the argument.
    """
    from sqlalchemy import select
    async with session_factory() as db:
        row = (
            await db.execute(
                select(AuditEvent)
                .where(AuditEvent.event_type == event_type)
                .order_by(AuditEvent.id)
                .limit(1)
            )
        ).scalar_one()
    assert row.request_id == _REQUEST_ID
    assert row.ip_address == _IP_ADDRESS


async def _first_audit_detail(session_factory, event_type: str) -> dict | None:
    """Return the `detail` JSON blob of the first audit row for the
    given event_type, or None if no row exists.
    """
    from sqlalchemy import select
    async with session_factory() as db:
        row = (
            await db.execute(
                select(AuditEvent)
                .where(AuditEvent.event_type == event_type)
                .order_by(AuditEvent.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        return row.detail if row is not None else None


# ── tests ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ai_simulation_falls_back_when_gate_closed(
    db, session_factory
):
    """When the ai.smart_plan feature gate is closed for the org,
    ``run_ai_simulation`` returns the analytic baseline unchanged.

    The engine_name on the response reflects the analytic baseline
    (no AI provenance block), and call_llm_structured is never
    invoked.
    """
    scenario = _make_retirement_scenario()
    req = _make_request(scenario)

    with patch(
        "app.services.scenario_engine_ai.feature_service.has_feature",
        AsyncMock(return_value=False),
    ), patch(
        "app.services.scenario_engine_ai.call_llm_structured",
        AsyncMock(),
    ) as mock_llm:
        result = await run_ai_simulation(
            db,
            session_factory=session_factory,
            org_id=1,
            user_id=1,
            actor_email="test@example.com",
            scenario=scenario,
            state=req.state,
            horizon_months=req.horizon_months,
            options=req.options,
            smooth_with_regression=False,
            request_id=_REQUEST_ID,
            ip_address=_IP_ADDRESS,
        )

    assert result["engine_name"] == "analytic_v1"
    assert result.get("ai_assumptions") is None
    mock_llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_ai_simulation_falls_back_when_no_routing(
    db, session_factory, seed_org_user
):
    """When AI gate is open but the org has no routing for smart_plan
    (raises NoRoutingConfigured at dispatch), the wrapper returns the
    analytic baseline AND emits a failure audit row with
    reason="no_routing". No exception leaks to the caller.
    """
    scenario = _make_retirement_scenario(
        org_id=seed_org_user["org_id"], user_id=seed_org_user["user_id"]
    )
    req = _make_request(scenario)

    with patch(
        "app.services.scenario_engine_ai.feature_service.has_feature",
        AsyncMock(return_value=True),
    ), patch(
        "app.services.scenario_engine_ai.call_llm_structured",
        AsyncMock(side_effect=NoRoutingConfigured()),
    ):
        result = await run_ai_simulation(
            db,
            session_factory=session_factory,
            org_id=seed_org_user["org_id"],
            user_id=seed_org_user["user_id"],
            actor_email=seed_org_user["email"],
            scenario=scenario,
            state=req.state,
            horizon_months=req.horizon_months,
            options=req.options,
            smooth_with_regression=False,
            request_id=_REQUEST_ID,
            ip_address=_IP_ADDRESS,
        )

    assert result["engine_name"] == "analytic_v1"
    assert result.get("ai_assumptions") is None
    n = await _count_audit_rows(session_factory, "plans.scenario.ai_simulate")
    assert n == 1, "no_routing fallback must emit one audit row"


@pytest.mark.asyncio
async def test_ai_simulation_happy_path_applies_deltas(
    db, session_factory, seed_org_user
):
    """LLM returns valid deltas → wrapper re-runs analytic with adjusted
    assumptions. Output carries ``engine_name = "analytic_v1+ai_assumptions_v1"``
    and an ``ai_assumptions`` provenance block describing what changed.

    Crucially, the baseline projection (without deltas) and the
    AI-adjusted projection have measurably different projection
    numbers when the LLM bumps ``annual_return_pct``. The numeric
    diff is the load-bearing assertion — a wrapper that built the
    provenance block but forgot to re-run the analytic engine must
    fail this test.
    """
    scenario = _make_retirement_scenario(
        org_id=seed_org_user["org_id"], user_id=seed_org_user["user_id"]
    )
    req = _make_request(scenario)

    # Compute baseline independently so we can prove the AI path
    # actually moved the projection numbers.
    baseline_only = AnalyticEngine().simulate(
        SimulationRequest(
            scenario=scenario,
            state=req.state,
            horizon_months=req.horizon_months,
            options=req.options,
        )
    )

    delta_payload = {
        "adjustments": [
            {
                "field": "annual_return_pct",
                "old_value": "6.0",
                "new_value": "7.5",
                "reason": "Recent recurring income growth suggests headroom",
            },
            {
                "field": "inflation_pct",
                "old_value": "2.5",
                "new_value": "3.0",
                "reason": "Recurring expense growth above prior assumption",
            },
        ],
        "summary": "Adjusted retirement assumptions based on org cashflow.",
    }

    fake_resp = StructuredDispatchResult(
        response=StructuredResponse(
            parsed=delta_payload,
            raw_text="{}",
            prompt_tokens=10,
            completion_tokens=10,
            model="gpt-4o-mini",
            retries_used=0,
        ),
        ledger_id=1,
    )

    with patch(
        "app.services.scenario_engine_ai.feature_service.has_feature",
        AsyncMock(return_value=True),
    ), patch(
        "app.services.scenario_engine_ai.call_llm_structured",
        AsyncMock(return_value=fake_resp),
    ):
        result = await run_ai_simulation(
            db,
            session_factory=session_factory,
            org_id=seed_org_user["org_id"],
            user_id=seed_org_user["user_id"],
            actor_email=seed_org_user["email"],
            scenario=scenario,
            state=req.state,
            horizon_months=req.horizon_months,
            options=req.options,
            smooth_with_regression=False,
            request_id=_REQUEST_ID,
            ip_address=_IP_ADDRESS,
        )

    assert result["engine_name"] == "analytic_v1+ai_assumptions_v1"
    assert "ai_assumptions" in result
    provenance = result["ai_assumptions"]
    assert provenance["summary"] == delta_payload["summary"]
    assert len(provenance["adjustments"]) == 2
    fields_changed = {a["field"] for a in provenance["adjustments"]}
    assert fields_changed == {"annual_return_pct", "inflation_pct"}

    # Load-bearing: the analytic engine actually ran with the new
    # assumptions. Comparing the projection content (after stripping
    # provenance / engine-name / timestamps that differ by design)
    # asserts the numbers changed.
    def _projection_content(r: dict) -> dict:
        return {
            k: v for k, v in r.items()
            if k not in ("engine_name", "ai_assumptions", "computed_at")
        }
    assert _projection_content(result) != _projection_content(baseline_only), (
        "AI-adjusted projection numbers must differ from baseline when "
        "annual_return_pct and inflation_pct were applied"
    )

    n = await _count_audit_rows(session_factory, "plans.scenario.ai_simulate")
    assert n == 1, "successful AI invocation must emit one audit row"


@pytest.mark.asyncio
async def test_ai_simulation_writes_audit_on_success(
    db, session_factory, seed_org_user
):
    """Successful AI invocation emits an ``plans.scenario.ai_simulate``
    audit row with outcome="success" and the delta count in detail.
    """
    scenario = _make_retirement_scenario(
        org_id=seed_org_user["org_id"], user_id=seed_org_user["user_id"]
    )
    req = _make_request(scenario)

    delta_payload = {
        "adjustments": [
            {
                "field": "annual_return_pct",
                "old_value": "6.0",
                "new_value": "7.5",
                "reason": "test",
            }
        ],
        "summary": "test summary",
    }
    fake_resp = StructuredDispatchResult(
        response=StructuredResponse(
            parsed=delta_payload,
            raw_text="{}",
            prompt_tokens=10,
            completion_tokens=10,
            model="m",
            retries_used=0,
        ),
        ledger_id=42,
    )

    with patch(
        "app.services.scenario_engine_ai.feature_service.has_feature",
        AsyncMock(return_value=True),
    ), patch(
        "app.services.scenario_engine_ai.call_llm_structured",
        AsyncMock(return_value=fake_resp),
    ):
        await run_ai_simulation(
            db,
            session_factory=session_factory,
            org_id=seed_org_user["org_id"],
            user_id=seed_org_user["user_id"],
            actor_email=seed_org_user["email"],
            scenario=scenario,
            state=req.state,
            horizon_months=req.horizon_months,
            options=req.options,
            smooth_with_regression=False,
            request_id=_REQUEST_ID,
            ip_address=_IP_ADDRESS,
        )

    n = await _count_audit_rows(session_factory, "plans.scenario.ai_simulate")
    assert n == 1
    await _assert_audit_context(session_factory, "plans.scenario.ai_simulate")


@pytest.mark.asyncio
async def test_ai_simulation_writes_audit_on_fallback(
    db, session_factory, seed_org_user
):
    """Fallback paths (StructuredOutputError, AIDispatchFailed) ALSO emit
    an audit row with outcome="failure" so an operator can see how
    often the AI path degrades. The frontend still gets a clean
    analytic projection.
    """
    scenario = _make_retirement_scenario(
        org_id=seed_org_user["org_id"], user_id=seed_org_user["user_id"]
    )
    req = _make_request(scenario)

    with patch(
        "app.services.scenario_engine_ai.feature_service.has_feature",
        AsyncMock(return_value=True),
    ), patch(
        "app.services.scenario_engine_ai.call_llm_structured",
        AsyncMock(side_effect=StructuredOutputError("STATUS_ERROR_STRUCTURED_OUTPUT")),
    ):
        result = await run_ai_simulation(
            db,
            session_factory=session_factory,
            org_id=seed_org_user["org_id"],
            user_id=seed_org_user["user_id"],
            actor_email=seed_org_user["email"],
            scenario=scenario,
            state=req.state,
            horizon_months=req.horizon_months,
            options=req.options,
            smooth_with_regression=False,
            request_id=_REQUEST_ID,
            ip_address=_IP_ADDRESS,
        )

    assert result["engine_name"] == "analytic_v1"
    assert result.get("ai_assumptions") is None
    n = await _count_audit_rows(session_factory, "plans.scenario.ai_simulate")
    assert n == 1
    await _assert_audit_context(session_factory, "plans.scenario.ai_simulate")


@pytest.mark.asyncio
async def test_ai_simulation_falls_back_on_dispatch_failed(
    db, session_factory, seed_org_user
):
    """Adapter/provider errors surface as ``AIDispatchFailed`` from the
    dispatcher. The wrapper swallows them, returns analytic, AND
    writes a failure audit row.
    """
    scenario = _make_retirement_scenario(
        org_id=seed_org_user["org_id"], user_id=seed_org_user["user_id"]
    )
    req = _make_request(scenario)

    with patch(
        "app.services.scenario_engine_ai.feature_service.has_feature",
        AsyncMock(return_value=True),
    ), patch(
        "app.services.scenario_engine_ai.call_llm_structured",
        AsyncMock(side_effect=AIDispatchFailed("connection_error")),
    ):
        result = await run_ai_simulation(
            db,
            session_factory=session_factory,
            org_id=seed_org_user["org_id"],
            user_id=seed_org_user["user_id"],
            actor_email=seed_org_user["email"],
            scenario=scenario,
            state=req.state,
            horizon_months=req.horizon_months,
            options=req.options,
            smooth_with_regression=False,
            request_id=_REQUEST_ID,
            ip_address=_IP_ADDRESS,
        )

    assert result["engine_name"] == "analytic_v1"
    assert result.get("ai_assumptions") is None
    n = await _count_audit_rows(session_factory, "plans.scenario.ai_simulate")
    assert n == 1, "dispatch-failure fallback must emit one audit row"


@pytest.mark.asyncio
async def test_ai_simulation_falls_back_on_schema_mismatch(
    db, session_factory, seed_org_user
):
    """An LLM that returns a JSON object that DOESN'T match
    ``AIAssumptionDelta`` (e.g. wrong field types, missing required
    keys) is rejected by the wrapper's Pydantic re-validation and the
    analytic baseline is returned. The dispatcher's response_schema
    catches structural mismatch up-front, but the Pydantic re-validate
    is the strict tripwire. A failure audit row is written.
    """
    scenario = _make_retirement_scenario(
        org_id=seed_org_user["org_id"], user_id=seed_org_user["user_id"]
    )
    req = _make_request(scenario)

    # Provide a payload that passes the dispatcher's loose schema check
    # (has "adjustments" key) but fails the strict Pydantic model
    # (each adjustment must have field/old_value/new_value/reason —
    # here we ship a malformed adjustment).
    bad_payload = {
        "adjustments": [{"oops": "this is not valid"}],
        "summary": "x",
    }
    fake_resp = StructuredDispatchResult(
        response=StructuredResponse(
            parsed=bad_payload,
            raw_text="{}",
            prompt_tokens=1,
            completion_tokens=1,
            model="m",
            retries_used=0,
        ),
        ledger_id=1,
    )

    with patch(
        "app.services.scenario_engine_ai.feature_service.has_feature",
        AsyncMock(return_value=True),
    ), patch(
        "app.services.scenario_engine_ai.call_llm_structured",
        AsyncMock(return_value=fake_resp),
    ):
        result = await run_ai_simulation(
            db,
            session_factory=session_factory,
            org_id=seed_org_user["org_id"],
            user_id=seed_org_user["user_id"],
            actor_email=seed_org_user["email"],
            scenario=scenario,
            state=req.state,
            horizon_months=req.horizon_months,
            options=req.options,
            smooth_with_regression=False,
            request_id=_REQUEST_ID,
            ip_address=_IP_ADDRESS,
        )

    assert result["engine_name"] == "analytic_v1"
    assert result.get("ai_assumptions") is None
    n = await _count_audit_rows(session_factory, "plans.scenario.ai_simulate")
    assert n == 1, "schema-mismatch fallback must emit one audit row"

    # The audit detail must include a compact validation-error summary
    # so an operator can tell at-a-glance why the LLM was rejected.
    detail = await _first_audit_detail(
        session_factory, "plans.scenario.ai_simulate"
    )
    assert detail is not None
    assert detail["reason"] == "schema_invalid"
    assert "validation_errors" in detail, (
        "schema_invalid audit row must include a validation_errors summary"
    )
    assert detail["validation_errors"]["count"] >= 1
    assert detail["validation_errors"]["first_type"] is not None


@pytest.mark.asyncio
async def test_ai_simulation_unknown_field_is_skipped(
    db, session_factory, seed_org_user
):
    """The Pydantic model accepts the LLM's payload, but the wrapper
    only applies adjustments to a whitelist of known assumption fields
    (annual_return_pct, inflation_pct, monthly_contribution). An
    adjustment naming a non-whitelisted field is dropped before the
    re-simulate; the result still flags this in provenance so an
    operator can see the LLM tried but the wrapper guarded against
    arbitrary param mutation. The whitelisted adjustment that
    survives must actually change the projection numbers (proves the
    re-simulate ran with the new value).
    """
    scenario = _make_retirement_scenario(
        org_id=seed_org_user["org_id"], user_id=seed_org_user["user_id"]
    )
    req = _make_request(scenario)
    baseline_only = AnalyticEngine().simulate(
        SimulationRequest(
            scenario=scenario,
            state=req.state,
            horizon_months=req.horizon_months,
            options=req.options,
        )
    )

    delta_payload = {
        "adjustments": [
            {
                "field": "evil_field",
                "old_value": "any",
                "new_value": "any",
                "reason": "try to inject",
            },
            {
                "field": "annual_return_pct",
                "old_value": "6.0",
                "new_value": "8.0",
                "reason": "ok",
            },
        ],
        "summary": "mixed",
    }
    fake_resp = StructuredDispatchResult(
        response=StructuredResponse(
            parsed=delta_payload,
            raw_text="{}",
            prompt_tokens=1,
            completion_tokens=1,
            model="m",
            retries_used=0,
        ),
        ledger_id=1,
    )

    with patch(
        "app.services.scenario_engine_ai.feature_service.has_feature",
        AsyncMock(return_value=True),
    ), patch(
        "app.services.scenario_engine_ai.call_llm_structured",
        AsyncMock(return_value=fake_resp),
    ):
        result = await run_ai_simulation(
            db,
            session_factory=session_factory,
            org_id=seed_org_user["org_id"],
            user_id=seed_org_user["user_id"],
            actor_email=seed_org_user["email"],
            scenario=scenario,
            state=req.state,
            horizon_months=req.horizon_months,
            options=req.options,
            smooth_with_regression=False,
            request_id=_REQUEST_ID,
            ip_address=_IP_ADDRESS,
        )

    # The AI path engaged (engine name reflects it) and only the
    # whitelisted field made it through.
    assert result["engine_name"] == "analytic_v1+ai_assumptions_v1"
    applied_fields = {
        a["field"] for a in result["ai_assumptions"]["adjustments"]
        if a.get("applied") is True
    }
    skipped_fields = {
        a["field"] for a in result["ai_assumptions"]["adjustments"]
        if a.get("applied") is False
    }
    assert "annual_return_pct" in applied_fields
    assert "evil_field" in skipped_fields

    # The whitelisted adjustment actually changed the projection.
    def _projection_content(r: dict) -> dict:
        return {
            k: v for k, v in r.items()
            if k not in ("engine_name", "ai_assumptions", "computed_at")
        }
    assert _projection_content(result) != _projection_content(baseline_only), (
        "applied=True on annual_return_pct must move the projection numbers"
    )

    n = await _count_audit_rows(session_factory, "plans.scenario.ai_simulate")
    assert n == 1


@pytest.mark.asyncio
async def test_ai_simulation_falls_back_when_gate_check_raises(
    db, session_factory, seed_org_user
):
    """A transient error while reading the feature gate (e.g. DB blip)
    must degrade to the analytic baseline WITHOUT writing an audit row
    — the audit pipeline only fires after the gate check passes
    (\"audit fires only when the AI path was at least attempted\").
    Pins the asymmetry so a future refactor of the bare-except can't
    silently start emitting audits or 500s on gate-check failure.
    """
    scenario = _make_retirement_scenario(
        org_id=seed_org_user["org_id"], user_id=seed_org_user["user_id"]
    )
    req = _make_request(scenario)

    with patch(
        "app.services.scenario_engine_ai.feature_service.has_feature",
        AsyncMock(side_effect=RuntimeError("db blip")),
    ), patch(
        "app.services.scenario_engine_ai.call_llm_structured",
        AsyncMock(),
    ) as mock_llm:
        result = await run_ai_simulation(
            db,
            session_factory=session_factory,
            org_id=seed_org_user["org_id"],
            user_id=seed_org_user["user_id"],
            actor_email=seed_org_user["email"],
            scenario=scenario,
            state=req.state,
            horizon_months=req.horizon_months,
            options=req.options,
            smooth_with_regression=False,
            request_id=_REQUEST_ID,
            ip_address=_IP_ADDRESS,
        )

    assert result["engine_name"] == "analytic_v1"
    assert result.get("ai_assumptions") is None
    mock_llm.assert_not_awaited()
    n = await _count_audit_rows(session_factory, "plans.scenario.ai_simulate")
    assert n == 0, "gate-check failure must NOT write an audit row"


@pytest.mark.asyncio
async def test_ai_simulation_applied_count_zero_returns_baseline_with_audit(
    db, session_factory, seed_org_user
):
    """If the LLM responds successfully but every proposed adjustment
    is non-applicable (all non-whitelisted, all out-of-range, etc.),
    the wrapper returns the analytic baseline AND writes an audit
    row recording ``reason=\"no_applicable_adjustments\"``. Pins the
    branch that's otherwise easy to skip past.
    """
    scenario = _make_retirement_scenario(
        org_id=seed_org_user["org_id"], user_id=seed_org_user["user_id"]
    )
    req = _make_request(scenario)

    # All adjustments target non-whitelisted fields → applied_count == 0.
    delta_payload = {
        "adjustments": [
            {
                "field": "totally_not_a_real_field",
                "old_value": "x",
                "new_value": "y",
                "reason": "off-list",
            },
            {
                "field": "another_bad_field",
                "old_value": "x",
                "new_value": "y",
                "reason": "off-list",
            },
        ],
        "summary": "the LLM had nothing useful to say",
    }
    fake_resp = StructuredDispatchResult(
        response=StructuredResponse(
            parsed=delta_payload,
            raw_text="{}",
            prompt_tokens=1,
            completion_tokens=1,
            model="m",
            retries_used=0,
        ),
        ledger_id=1,
    )

    with patch(
        "app.services.scenario_engine_ai.feature_service.has_feature",
        AsyncMock(return_value=True),
    ), patch(
        "app.services.scenario_engine_ai.call_llm_structured",
        AsyncMock(return_value=fake_resp),
    ):
        result = await run_ai_simulation(
            db,
            session_factory=session_factory,
            org_id=seed_org_user["org_id"],
            user_id=seed_org_user["user_id"],
            actor_email=seed_org_user["email"],
            scenario=scenario,
            state=req.state,
            horizon_months=req.horizon_months,
            options=req.options,
            smooth_with_regression=False,
            request_id=_REQUEST_ID,
            ip_address=_IP_ADDRESS,
        )

    # engine_name reflects baseline (no AI provenance attached).
    assert result["engine_name"] == "analytic_v1"
    assert result.get("ai_assumptions") is None
    n = await _count_audit_rows(session_factory, "plans.scenario.ai_simulate")
    assert n == 1, "no_applicable_adjustments must still emit one audit row"


@pytest.mark.asyncio
async def test_ai_simulation_does_not_mutate_input_scenario_params(
    db, session_factory, seed_org_user
):
    """The wrapper temporarily swaps ``scenario.params_json`` to feed
    the adjusted assumptions through the analytic engine, then
    restores in a ``finally`` block. This test pins that the input
    Scenario object's ``params_json`` is unchanged after the call —
    a regression that bypassed the restore would leak LLM
    mutations back into the caller's session.
    """
    scenario = _make_retirement_scenario(
        org_id=seed_org_user["org_id"], user_id=seed_org_user["user_id"]
    )
    req = _make_request(scenario)
    original_params = deepcopy(scenario.params_json)

    delta_payload = {
        "adjustments": [
            {
                "field": "annual_return_pct",
                "old_value": "6.0",
                "new_value": "7.5",
                "reason": "headroom",
            }
        ],
        "summary": "bump return",
    }
    fake_resp = StructuredDispatchResult(
        response=StructuredResponse(
            parsed=delta_payload,
            raw_text="{}",
            prompt_tokens=1,
            completion_tokens=1,
            model="m",
            retries_used=0,
        ),
        ledger_id=1,
    )

    with patch(
        "app.services.scenario_engine_ai.feature_service.has_feature",
        AsyncMock(return_value=True),
    ), patch(
        "app.services.scenario_engine_ai.call_llm_structured",
        AsyncMock(return_value=fake_resp),
    ):
        result = await run_ai_simulation(
            db,
            session_factory=session_factory,
            org_id=seed_org_user["org_id"],
            user_id=seed_org_user["user_id"],
            actor_email=seed_org_user["email"],
            scenario=scenario,
            state=req.state,
            horizon_months=req.horizon_months,
            options=req.options,
            smooth_with_regression=False,
            request_id=_REQUEST_ID,
            ip_address=_IP_ADDRESS,
        )

    # Sanity: the adjustment was actually applied (otherwise the
    # restore test is vacuous — there'd be nothing to restore).
    assert result["engine_name"] == "analytic_v1+ai_assumptions_v1"
    # Load-bearing: the caller's scenario object is unmutated.
    assert scenario.params_json == original_params, (
        "wrapper must restore scenario.params_json before returning; the "
        "finally block at scenario_engine_ai.py:362-363 is load-bearing"
    )


@pytest.mark.asyncio
async def test_ai_simulation_out_of_bounds_value_is_skipped(
    db, session_factory, seed_org_user
):
    """Per-field numeric bounds reject out-of-range LLM proposals
    (e.g. ``annual_return_pct: \"500.0\"``, ``monthly_contribution:
    \"-1000.0\"``). Whitelisted but out-of-range adjustments land
    in provenance with ``applied=False`` and never reach the analytic
    engine.
    """
    scenario = _make_retirement_scenario(
        org_id=seed_org_user["org_id"], user_id=seed_org_user["user_id"]
    )
    req = _make_request(scenario)

    delta_payload = {
        "adjustments": [
            {
                "field": "annual_return_pct",
                "old_value": "6.0",
                "new_value": "500.0",  # > 50 upper bound
                "reason": "hallucinated outsized return",
            },
            {
                "field": "monthly_contribution",
                "old_value": "500.00",
                "new_value": "-1000.00",  # < 0 lower bound
                "reason": "negative contribution",
            },
            {
                "field": "inflation_pct",
                "old_value": "2.5",
                "new_value": "not_a_number",  # non-numeric
                "reason": "garbage",
            },
        ],
        "summary": "everything out of range",
    }
    fake_resp = StructuredDispatchResult(
        response=StructuredResponse(
            parsed=delta_payload,
            raw_text="{}",
            prompt_tokens=1,
            completion_tokens=1,
            model="m",
            retries_used=0,
        ),
        ledger_id=1,
    )

    with patch(
        "app.services.scenario_engine_ai.feature_service.has_feature",
        AsyncMock(return_value=True),
    ), patch(
        "app.services.scenario_engine_ai.call_llm_structured",
        AsyncMock(return_value=fake_resp),
    ):
        result = await run_ai_simulation(
            db,
            session_factory=session_factory,
            org_id=seed_org_user["org_id"],
            user_id=seed_org_user["user_id"],
            actor_email=seed_org_user["email"],
            scenario=scenario,
            state=req.state,
            horizon_months=req.horizon_months,
            options=req.options,
            smooth_with_regression=False,
            request_id=_REQUEST_ID,
            ip_address=_IP_ADDRESS,
        )

    # All three were rejected → applied_count == 0 → analytic baseline.
    assert result["engine_name"] == "analytic_v1"
    assert result.get("ai_assumptions") is None
    n = await _count_audit_rows(session_factory, "plans.scenario.ai_simulate")
    assert n == 1, "bounds-rejected-all must emit one audit row"


def test_ai_assumption_delta_schema_has_required_keys():
    """Sanity: the schema we hand to the dispatcher must list the keys
    the dispatcher's structural validator requires."""
    assert AI_ASSUMPTION_SCHEMA["type"] == "object"
    assert "adjustments" in AI_ASSUMPTION_SCHEMA.get("required", [])


def test_ai_assumption_delta_model_round_trips():
    payload = {
        "adjustments": [
            {
                "field": "annual_return_pct",
                "old_value": "6.0",
                "new_value": "7.5",
                "reason": "test",
            }
        ],
        "summary": "ok",
    }
    model = AIAssumptionDelta.model_validate(payload)
    assert model.summary == "ok"
    assert model.adjustments[0].field == "annual_return_pct"
