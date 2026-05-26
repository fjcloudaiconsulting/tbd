"""AI-enhanced Plans simulation orchestrator (PR4 of the Plans train).

Spec: ``specs/2026-05-22-plans-page-simulation-sandbox.md`` §"Simulation
engine, AI path (layered)".

Architectural shape:

- ``AIEngine`` is the registry stub (sync; returns the analytic baseline
  for safety when invoked without a DB context). The real AI flow runs
  through ``run_ai_simulation`` below, which the router calls when the
  request asks for ``engine="ai_enhanced"``.
- The wrapper is purely additive — it never replaces the analytic
  engine, it just adjusts a small whitelisted set of assumptions
  (annual_return_pct, inflation_pct, monthly_contribution) and
  re-runs the analytic engine. The output shape is identical; only the
  numbers and an additional ``ai_assumptions`` provenance block
  differ.
- Every failure mode falls back to the analytic baseline. The frontend
  must never crash because an LLM call timed out, returned malformed
  JSON, or had its credentials revoked.
- Every AI invocation (success OR fallback) emits an audit event so
  operators can see how often the path engages and how often it
  degrades.

The AI feature gate (``ai.smart_plan``) AND a configured ``smart_plan``
routing row are BOTH required. Gate off → analytic-only (no LLM call).
Routing missing or any dispatch error → analytic-only with an audit row
recording the fallback reason.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import structlog
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.scenario import Scenario
from app.services import audit_service, feature_service
from app.services.ai_dispatch import (
    AICapExceeded,
    AIDispatchError,
    AIDispatchFailed,
    NoRoutingConfigured,
    call_llm_structured,
)
from app.services.ai_providers.base import (
    NativeNotAvailable,
    StructuredOutputError,
)
from app.services.scenario_engine import (
    AnalyticEngine,
    SimulationRequest,
    WorldState,
)


logger = structlog.stdlib.get_logger()


# Feature-gate key (see app/auth/feature_catalog.py). Gates AI-enhanced
# plans behind an org-level entitlement so even an org with valid BYOK
# credentials must opt in.
AI_PLAN_FEATURE_KEY = "ai.smart_plan"

# Routable feature name used by ai_routing_service / ai_dispatch.
# Matches the entry in ``ROUTABLE_FEATURE_NAMES`` in
# ``app/models/org_ai_routing.py`` ("smart_plan", since "ai." is the
# entitlement-catalog prefix, not the routing key).
ROUTING_FEATURE_KEY = "smart_plan"

# Audit-event type for this slice. Single event-type emitted on every
# AI invocation regardless of outcome; the ``outcome`` field
# (success/failure) carries the disposition.
AUDIT_EVENT_TYPE = "plans.scenario.ai_simulate"

# Whitelist of params_json keys the wrapper is allowed to mutate. An
# LLM-proposed adjustment to any field outside this set is logged as
# "skipped" in provenance but never applied. Deliberately narrow —
# the analytic baseline does the projection math; AI only nudges the
# numeric assumptions a human user would have set manually.
ASSUMPTION_FIELD_WHITELIST: frozenset[str] = frozenset(
    {
        "annual_return_pct",
        "inflation_pct",
        "monthly_contribution",
    }
)

# Per-field numeric bounds for LLM-proposed values. The whitelist
# guards WHICH fields can change; these bounds guard WHAT VALUES are
# allowed. An LLM hallucinating a 500% annual return or a negative
# monthly contribution silently distorts the projection — out-of-range
# values are rejected before the analytic engine sees them. Ranges are
# intentionally permissive (e.g. allow negative annual return for
# declining-asset scenarios, allow up to 50% inflation for stress
# tests) so legitimate edge cases still pass.
_FIELD_BOUNDS: dict[str, tuple[Decimal, Decimal]] = {
    "annual_return_pct": (Decimal("-50"), Decimal("50")),
    "inflation_pct": (Decimal("-10"), Decimal("50")),
    "monthly_contribution": (Decimal("0"), Decimal("1000000")),
}

# JSON schema we hand to ``call_llm_structured``. The dispatcher's
# structural validator only checks ``type``/``required`` (a deeper
# validator is reserved for a future PR), so this is intentionally
# the minimum the dispatcher needs to retry-on-mismatch. The strict
# Pydantic re-validate below is the real tripwire.
AI_ASSUMPTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["adjustments"],
    "properties": {
        "adjustments": {"type": "array"},
        "summary": {"type": "string"},
    },
}


# ── Pydantic models for the LLM contract ───────────────────────────────


class AIAssumptionAdjustment(BaseModel):
    """One assumption-level adjustment the LLM proposes.

    ``field`` is the params_json key the LLM wants to change. The
    wrapper enforces an allow-list (``ASSUMPTION_FIELD_WHITELIST``)
    before applying; a non-whitelisted field is recorded in provenance
    as ``applied=False`` and never mutates the scenario.

    ``old_value`` and ``new_value`` are kept as strings so Decimal
    quantization round-trips losslessly. The LLM is asked to format
    them as decimal strings.
    """

    model_config = ConfigDict(extra="forbid")

    field: str
    old_value: str
    new_value: str
    reason: str


class AIAssumptionDelta(BaseModel):
    """Top-level Pydantic model for the LLM's structured response.

    ``extra="forbid"`` is intentional — a model that wanders off the
    schema is rejected so the wrapper falls back to analytic. We do
    NOT want creative additions silently shaping the projection.
    """

    model_config = ConfigDict(extra="forbid")

    adjustments: list[AIAssumptionAdjustment]
    summary: str = ""


# ── Public orchestrator ───────────────────────────────────────────────


async def run_ai_simulation(
    db: AsyncSession,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    org_id: int,
    user_id: int,
    actor_email: str,
    scenario: Scenario,
    state: WorldState,
    horizon_months: int,
    options: dict[str, Any],
    smooth_with_regression: bool = False,
    request_id: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> dict[str, Any]:
    """Run the AI-enhanced projection for ``scenario`` (or analytic
    fallback if AI is gated off / unavailable / errored).

    Output shape matches ``AnalyticEngine.simulate`` plus an optional
    ``ai_assumptions`` provenance block. ``engine_name`` reflects which
    path actually ran:

    - ``"analytic_v1"`` — AI gate closed, routing missing, dispatch
      error, schema mismatch, or zero whitelisted adjustments. The
      result is bit-for-bit identical to what
      ``AnalyticEngine.simulate`` would have produced.
    - ``"analytic_v1+ai_assumptions_v1"`` — at least one whitelisted
      adjustment was applied. The ``ai_assumptions`` block carries
      provenance for every adjustment the LLM proposed (applied or
      skipped) plus the model's ``summary``.

    The caller (router) writes the result back onto the Scenario row.
    This function never touches the row itself.
    """
    analytic = AnalyticEngine()
    baseline_request = SimulationRequest(
        scenario=scenario,
        state=state,
        horizon_months=horizon_months,
        options=options,
    )

    def _baseline() -> dict[str, Any]:
        """Compute the analytic baseline. Called only on fallback paths;
        the AI-success path re-runs analytic with adjusted params
        instead, so computing eagerly would double the simulator work
        for every AI-engaged request.
        """
        return analytic.simulate(
            baseline_request, smooth_with_regression=smooth_with_regression
        )

    # 1. Gate check. AI Tier off → return the baseline unchanged. No
    #    LLM call, no audit row (audit fires only when the AI path
    #    was at least attempted).
    try:
        gate_open = await feature_service.has_feature(
            db, org_id, AI_PLAN_FEATURE_KEY
        )
    except Exception as exc:  # noqa: BLE001 - defensive
        logger.warning(
            "plans.ai.gate_check.failed",
            org_id=org_id,
            scenario_id=scenario.id,
            error_class=type(exc).__name__,
        )
        return _baseline()
    if not gate_open:
        logger.info(
            "plans.ai.gate.closed",
            org_id=org_id,
            scenario_id=scenario.id,
        )
        return _baseline()

    # 2. Build the prompt and call the dispatcher. The dispatcher
    #    handles routing, cap enforcement, ledger writes, and retries
    #    on schema mismatch — we just consume its result.
    #
    # The dispatcher commits the session it's given (the ledger row
    # write does an `await db.commit()`). To keep that commit
    # isolated from the request transaction — and from any other
    # work the wrapper might stage on `db` — we open a dedicated
    # session for the dispatch call. Same pattern as the audit
    # pipeline below.
    prompt_messages = _build_prompt_messages(scenario, state, horizon_months)
    fallback_reason: Optional[str] = None
    validation_errors: Optional[dict[str, Any]] = None
    delta: Optional[AIAssumptionDelta] = None

    try:
        async with session_factory() as dispatch_db:
            dispatch_result = await call_llm_structured(
                dispatch_db,
                org_id=org_id,
                feature_key=ROUTING_FEATURE_KEY,
                messages=prompt_messages,
                response_schema=AI_ASSUMPTION_SCHEMA,
            )
    except NoRoutingConfigured:
        fallback_reason = "no_routing"
    except AICapExceeded:
        fallback_reason = "cap_exceeded"
    except NativeNotAvailable:
        fallback_reason = "native_unavailable"
    except StructuredOutputError:
        fallback_reason = "structured_output_exhausted"
    except AIDispatchFailed as exc:
        fallback_reason = f"dispatch_failed:{exc.code}"
    except AIDispatchError as exc:
        # Catch-all for any future typed dispatch error not enumerated
        # above. Keeping this LAST ensures the more specific handlers
        # win for the cases we care about, and a new error type
        # doesn't slip past unnoticed (the structured log surfaces it).
        fallback_reason = f"dispatch_error:{exc.code}"
    except Exception as exc:  # noqa: BLE001 - defensive fallback
        # Truly unexpected — the dispatcher should map known failure
        # modes to typed errors. Log loudly and degrade gracefully.
        logger.warning(
            "plans.ai.dispatch.unexpected_error",
            org_id=org_id,
            scenario_id=scenario.id,
            error_class=type(exc).__name__,
        )
        fallback_reason = f"unexpected:{type(exc).__name__}"
    else:
        # Parse strictly via Pydantic. The dispatcher's structural
        # validator only checks top-level required keys; the Pydantic
        # model rejects malformed adjustment objects (the real
        # tripwire). On parse failure we fall back to analytic and
        # surface a compact error summary in the audit row's detail
        # so an operator can tell at-a-glance why the LLM was
        # rejected without having to grep logs.
        try:
            delta = AIAssumptionDelta.model_validate(
                dispatch_result.response.parsed
            )
        except ValidationError as exc:
            errs = exc.errors()
            validation_errors = {
                "count": len(errs),
                "first_type": errs[0].get("type") if errs else None,
                "first_loc": (
                    ".".join(str(p) for p in errs[0].get("loc", ()))
                    if errs
                    else None
                ),
            }
            logger.warning(
                "plans.ai.schema.invalid",
                org_id=org_id,
                scenario_id=scenario.id,
                **validation_errors,
            )
            fallback_reason = "schema_invalid"

    # 3. If anything went wrong, return baseline + audit the fallback.
    if delta is None:
        failure_detail: dict[str, Any] = {
            "scenario_id": scenario.id,
            "reason": fallback_reason or "unknown",
            "applied_adjustments": 0,
        }
        if validation_errors is not None:
            failure_detail["validation_errors"] = validation_errors
        await _record_audit(
            session_factory,
            org_id=org_id,
            user_id=user_id,
            actor_email=actor_email,
            scenario=scenario,
            outcome="failure",
            detail=failure_detail,
            request_id=request_id,
            ip_address=ip_address,
        )
        return _baseline()

    # 4. Apply whitelisted adjustments to a deep-copied params blob and
    #    re-run the analytic engine. Track every adjustment in
    #    provenance with an ``applied`` flag so operators see what
    #    the LLM proposed even when the wrapper guarded against it.
    new_params = deepcopy(scenario.params_json or {})
    provenance_adjustments: list[dict[str, Any]] = []
    applied_count = 0
    for adj in delta.adjustments:
        is_whitelisted = adj.field in ASSUMPTION_FIELD_WHITELIST
        applied = is_whitelisted and _is_within_bounds(adj.field, adj.new_value)
        if applied:
            new_params[adj.field] = adj.new_value
            applied_count += 1
        provenance_adjustments.append(
            {
                "field": adj.field,
                "old_value": adj.old_value,
                "new_value": adj.new_value,
                "reason": adj.reason,
                "applied": applied,
            }
        )

    if applied_count == 0:
        # Nothing actually changed. Surface this as a fallback in the
        # audit row but return the baseline — the engine_name should
        # honestly reflect that AI didn't move the numbers.
        await _record_audit(
            session_factory,
            org_id=org_id,
            user_id=user_id,
            actor_email=actor_email,
            scenario=scenario,
            outcome="success",
            detail={
                "scenario_id": scenario.id,
                "reason": "no_applicable_adjustments",
                "proposed": len(provenance_adjustments),
                "applied_adjustments": 0,
            },
            request_id=request_id,
            ip_address=ip_address,
        )
        return _baseline()

    # 5. Re-run analytic engine with the adjusted params. The scenario
    #    object's params_json is mutated in-place for the duration of
    #    this call — but since SQLAlchemy hasn't committed, and the
    #    router writes back the projection separately, this DOES NOT
    #    persist to the DB.
    #
    # Use a shallow override technique: temporarily swap params_json,
    # simulate, then restore. This avoids constructing a new Scenario
    # instance (which would risk losing FK/state semantics).
    original_params = scenario.params_json
    scenario.params_json = new_params
    try:
        adjusted_result = analytic.simulate(
            SimulationRequest(
                scenario=scenario,
                state=state,
                horizon_months=horizon_months,
                options=options,
            ),
            smooth_with_regression=smooth_with_regression,
        )
    finally:
        scenario.params_json = original_params

    adjusted_result["engine_name"] = "analytic_v1+ai_assumptions_v1"
    adjusted_result["ai_assumptions"] = {
        "summary": delta.summary,
        "adjustments": provenance_adjustments,
        "applied_count": applied_count,
        "proposed_count": len(provenance_adjustments),
    }

    await _record_audit(
        session_factory,
        org_id=org_id,
        user_id=user_id,
        actor_email=actor_email,
        scenario=scenario,
        outcome="success",
        detail={
            "scenario_id": scenario.id,
            "applied_adjustments": applied_count,
            "proposed_adjustments": len(provenance_adjustments),
        },
        request_id=request_id,
        ip_address=ip_address,
    )
    logger.info(
        "plans.ai.success",
        org_id=org_id,
        scenario_id=scenario.id,
        applied=applied_count,
        proposed=len(provenance_adjustments),
    )
    return adjusted_result


# ── Internal helpers ───────────────────────────────────────────────────


def _build_prompt_messages(
    scenario: Scenario, state: WorldState, horizon_months: int
) -> list[dict[str, str]]:
    """Build the LLM messages for the assumption-adjustment call.

    Privacy notes:

    - We send aggregated cashflow trend data (12-month per-account
      net) but NEVER individual transaction descriptions, merchant
      names, counterparty info, or account labels. The LLM only sees
      numeric trends + the current assumptions on the scenario.
    - Account IDs are sent as ordinal indices, not the real DB ids,
      to keep the prompt portable across re-runs.

    The LLM is constrained to return a strict JSON object via the
    structured-output capability (dispatcher applies the schema and
    retries on parse failure).
    """
    stype = (
        scenario.scenario_type.value
        if hasattr(scenario.scenario_type, "value")
        else str(scenario.scenario_type)
    )
    params = scenario.params_json or {}
    current_assumptions = {
        k: params.get(k)
        for k in ASSUMPTION_FIELD_WHITELIST
        if params.get(k) is not None
    }

    # Aggregate the 12-month history into a per-account net-monthly
    # trend (sum, mean, count). Avoid raw monthly data so the prompt
    # stays compact and doesn't leak per-month patterns that could
    # be reverse-engineered to a single transaction.
    history_by_acc: dict[int, list[str]] = {}
    for pt in state.history:
        history_by_acc.setdefault(pt.account_id, []).append(str(pt.net))
    trend_summary = []
    for idx, (_acc_id, nets) in enumerate(sorted(history_by_acc.items())):
        # Compact summary; the LLM does not need account IDs.
        try:
            decs = [Decimal(n) for n in nets]
            avg = sum(decs, Decimal("0")) / Decimal(len(decs)) if decs else Decimal("0")
            trend_summary.append(
                {
                    "account_ordinal": idx,
                    "months_observed": len(decs),
                    "avg_net": str(avg.quantize(Decimal("0.01"))),
                }
            )
        except InvalidOperation:
            continue

    system_msg = (
        "You are a careful financial-planning assistant. Given a "
        "scenario's current numeric assumptions and a short aggregated "
        "view of an organization's recent monthly cashflow, propose at "
        "most three adjustments to the assumption fields listed. Only "
        "propose values you can justify from the cashflow trend. Each "
        "adjustment must include the field name, the old value, the "
        "new value, and a brief reason. Output strict JSON matching "
        "the schema. Do not propose changes to fields outside the "
        "provided list."
    )
    user_msg = {
        "scenario_type": stype,
        "horizon_months": horizon_months,
        "current_assumptions": current_assumptions,
        "allowed_fields": sorted(ASSUMPTION_FIELD_WHITELIST),
        "cashflow_trend": trend_summary,
    }
    import json as _json
    return [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": _json.dumps(user_msg)},
    ]


def _is_within_bounds(field: str, value: str) -> bool:
    """True if ``value`` parses as a finite Decimal AND lies within
    the per-field bounds in ``_FIELD_BOUNDS``. Used to reject both
    non-numeric LLM outputs (e.g. ``"high"``) and out-of-range numeric
    proposals (e.g. ``"-50.0"`` for an annual return) before they
    reach Decimal math in ``AnalyticEngine``.

    Fields not in ``_FIELD_BOUNDS`` always return False — callers
    should pair this with the ``ASSUMPTION_FIELD_WHITELIST`` check.
    """
    if field not in _FIELD_BOUNDS:
        return False
    try:
        d = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return False
    if not d.is_finite():
        return False
    lo, hi = _FIELD_BOUNDS[field]
    return lo <= d <= hi


async def _record_audit(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    org_id: int,
    user_id: int,
    actor_email: str,
    scenario: Scenario,
    outcome: str,
    detail: dict[str, Any],
    request_id: Optional[str],
    ip_address: Optional[str],
) -> None:
    """Best-effort audit-event recording for this AI invocation."""
    await audit_service.record_audit_event(
        session_factory,
        event_type=AUDIT_EVENT_TYPE,
        actor_user_id=user_id,
        actor_email=actor_email,
        target_org_id=org_id,
        target_org_name=None,
        request_id=request_id,
        ip_address=ip_address,
        outcome=outcome,  # type: ignore[arg-type]
        detail=detail,
    )
