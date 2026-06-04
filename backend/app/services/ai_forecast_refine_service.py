"""LAI.2 — Smart Forecast refinement service.

Layers AI-detected seasonality + anomaly flags on top of the
deterministic forecast from ``forecast_service``. The flow is:

1. Compute the baseline via ``forecast_service.compute_forecast``.
2. Build an aggregated transaction summary (up to 12 months of history,
   user-configurable: 3, 6, or 12; per-category, monthly totals only,
   no raw transaction text, no merchant names, no descriptions). This
   is the prompt-builder's privacy boundary: we send aggregates, not rows.
3. Build the Prompt with the baseline + summary.
4. Dispatch via ``ai_dispatch.call_llm_structured`` with feature key
   ``"ai.forecast"`` and the ``AIForecastAdjustments`` JSON schema.
5. Validate the response via Pydantic. Malformed values fall back to
   baseline.
6. Apply per-category multipliers (already range-checked by Pydantic)
   and emit provenance.

Fallback contract: on ANY exception (gate closed, no routing, cap
exceeded, structured-output exhausted, validation failure) the service
returns the baseline forecast with ``provenance.ai_applied=False`` and
``fallback_reason`` set. The router never 5xxs because refinement is
purely additive.

Data sent to the LLM (privacy boundary documented here):
- Baseline forecast totals (income / expense Decimal aggregates).
- Per-category history: ``{name, period_label, total_expense}``.
  We DO NOT send: transaction memos, merchant names, individual
  amounts, account names, dates beyond period labels, user / account
  IDs.
- The org_id is not sent. Category names are user-typed, so an org
  could leak intent via "Pat's secret therapy fund". The caller is
  responsible for any further redaction at the category level.
"""
from __future__ import annotations

import datetime
import json
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

import structlog
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.category import Category
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.schemas.ai_forecast import (
    AIForecastAdjustments,
    ForecastRefineEstimate,
    RefinedCategoryRow,
    RefinedForecastProvenance,
    RefinedForecastResponse,
)
from app.services import ai_dispatch, ai_routing_service, forecast_service
from app.services.ai_forecast_refine_token_estimate import (
    Scope,
    _duration_band,
    estimate_output_tokens,
    estimate_prompt_tokens,
    max_tokens_for_output_estimate,
    select_categories_by_scope,
)
from app.services.ai_pricing import estimate_cost_cents
from app.services.ai_providers.base import NativeNotAvailable, StructuredOutputError
from app.services.transaction_filters import reportable_transaction_filter

logger = structlog.stdlib.get_logger()


# Two distinct keys for the same surface:
# - GATE_KEY is the entitlement-catalog key the router checks via
#   require_feature(); the value matches the AI tier flag the org pays for.
# - ROUTING_KEY is the routable feature name the dispatcher uses to look
#   up a per-feature routing row in ORG_AI_DEFAULT_ROUTING. It MUST be a
#   member of ROUTABLE_FEATURE_NAMES in app/models/org_ai_routing.py.
#   If you pass the gate key here, feature-specific routing rows are
#   silently missed and the dispatcher falls through to the default.
GATE_KEY = "ai.forecast"
ROUTING_KEY = "smart_forecast"

# Default ``months`` arg for ``_build_category_history``. Callers
# (``refine_forecast`` and ``estimate_refine``) always pass
# ``months=timeframe_months`` explicitly, so the DB query pulls exactly
# the requested window. No max-window-then-slice happens here.
# Categories with zero spend in the window are omitted.
HISTORY_MONTHS = 12


# JSON schema passed to call_llm_structured. We keep this in sync with
# the Pydantic model. The dispatcher's structured-output retry check
# only validates required keys + top-level type, so the Pydantic
# re-validation in this service is what catches subtle shape drift.
RESPONSE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["seasonal", "anomalies", "confidence", "summary"],
    "properties": {
        "seasonal": {"type": "array"},
        "anomalies": {"type": "array"},
        "confidence": {"type": "number"},
        "summary": {"type": "string"},
    },
}


def _month_start_n_back(end: datetime.date, n: int) -> datetime.date:
    """Return the first-of-month n months before ``end``'s month."""
    year = end.year
    month = end.month - n
    while month <= 0:
        month += 12
        year -= 1
    return datetime.date(year, month, 1)


async def _build_category_history(
    db: AsyncSession,
    *,
    org_id: int,
    period_start: datetime.date,
    months: int = HISTORY_MONTHS,
) -> list[dict]:
    """Build a per-category expense history up to ``months`` months back.

    Returns one row per (category, month) with the total expense. Used
    inside the LLM prompt as aggregates only, no transaction-level
    detail. Settled-only (status=SETTLED) because pending rows are
    speculative and would conflate noise with seasonality.

    The window ends STRICTLY before ``period_start`` — we don't want
    actuals from the forecast period itself bleeding into the
    seasonality signal, because the LLM's multiplier is supposed to
    *modify* the forecast, not learn from it.

    Month bucketing is done in Python so the query stays portable
    between SQLite (tests) and MySQL (production), where the
    ``YYYY-MM`` truncation functions diverge (``strftime`` vs.
    ``date_format``).
    """
    # End of the history window = the last full calendar month before
    # period_start. Start = `months` months back from that, on the 1st.
    history_start = _month_start_n_back(period_start, months)

    result = await db.execute(
        select(
            Transaction.category_id,
            Transaction.settled_date,
            Transaction.amount,
        )
        .where(
            Transaction.org_id == org_id,
            Transaction.type == TransactionType.EXPENSE,
            Transaction.status == TransactionStatus.SETTLED,
            Transaction.settled_date >= history_start,
            Transaction.settled_date < period_start,
            reportable_transaction_filter(),
        )
    )

    buckets: dict[tuple[Optional[int], str], Decimal] = {}
    for row in result.all():
        cat_id = int(row[0]) if row[0] is not None else None
        settled = row[1]
        amount = Decimal(str(row[2]))
        if settled is None:
            continue
        month_label = f"{settled.year:04d}-{settled.month:02d}"
        key = (cat_id, month_label)
        buckets[key] = buckets.get(key, Decimal("0")) + amount

    rows: list[dict] = []
    for (cat_id, month_label), total in sorted(
        buckets.items(), key=lambda kv: (kv[0][1], kv[0][0] or 0)
    ):
        rows.append(
            {
                "category_id": cat_id,
                "month": month_label,
                "total_expense": str(total),
            }
        )
    return rows


async def _category_index(db: AsyncSession, *, org_id: int) -> dict[int, str]:
    """Return ``{category_id: name}`` for the org. Used to label history
    rows in the prompt AND to label refined output rows.
    """
    result = await db.execute(
        select(Category.id, Category.name).where(Category.org_id == org_id)
    )
    return {int(row[0]): str(row[1]) for row in result.all()}


def _spend_by_category(history: list[dict]) -> dict[int, float]:
    """Roll up ``total_expense`` per category across the history window."""
    out: dict[int, float] = {}
    for row in history:
        cid = row["category_id"]
        if cid is None:
            continue
        out[cid] = out.get(cid, 0.0) + float(row["total_expense"])
    return out


def _system_instructions(timeframe_months: int) -> str:
    """Build a dynamic system prompt that includes the actual timeframe."""
    return (
        "You are a personal-finance forecasting assistant. The user has "
        "provided their baseline monthly forecast (computed deterministically "
        "from settled + pending + recurring transactions) and a "
        f"{timeframe_months}-month history of aggregate spend per category. "
        "Detect seasonal patterns and flag anomalies. Return ONLY a JSON object "
        "matching the AIForecastAdjustments schema. Multipliers MUST be between "
        "0.5 and 1.5. Confidence MUST be between 0.0 and 1.0. Each rationale MUST "
        "be under 240 characters and the summary under 480 characters. Each "
        "anomaly severity MUST be exactly one of: info, warning, alert. Treat "
        "category names as opaque labels. Do not invent categories that aren't in "
        "the input."
    )


def _build_refine_prompt(
    *,
    baseline: dict,
    history: list[dict],
    category_index: dict[int, str],
    timeframe_months: int,
    scope: Scope,
) -> tuple[list[dict], int, int]:
    """Build the messages array for the structured-output dispatch.

    Returns ``(messages, est_output_tokens, n_in_scope)``.

    Privacy note: ``baseline`` is the typed forecast dict from
    ``forecast_service.compute_forecast`` (string-serialized Decimals,
    category names, period labels). ``history`` is monthly aggregates
    only — no raw transaction text, no merchant names, no descriptions.
    Nothing else leaves this function.

    Only categories that fall within ``scope`` (by spend rank) are
    included in the prompt. This limits both token cost and the LLM's
    attack surface.
    """
    in_scope = set(select_categories_by_scope(_spend_by_category(history), scope))

    scoped_history = [r for r in history if r["category_id"] in in_scope]
    scoped_categories = [
        c for c in baseline.get("categories", [])
        if int(c["category_id"]) in in_scope
    ]

    history_with_names = [
        {
            "category_id": row["category_id"],
            "category_name": category_index.get(row["category_id"] or -1, "Unknown"),
            "month": row["month"],
            "total_expense": row["total_expense"],
        }
        for row in scoped_history
    ]
    user_payload = {
        "baseline_forecast": {
            "period_start": baseline["period_start"],
            "period_end": baseline["period_end"],
            "forecast_income": baseline["forecast_income"],
            "forecast_expense": baseline["forecast_expense"],
            "categories": scoped_categories,
        },
        "history": history_with_names,
    }
    messages = [
        {"role": "system", "content": _system_instructions(timeframe_months)},
        {"role": "user", "content": json.dumps(user_payload, default=str)},
    ]
    n_in_scope = len(in_scope)
    return messages, estimate_output_tokens(category_count=n_in_scope), n_in_scope


def _apply_adjustments(
    *, baseline: dict, adjustments: AIForecastAdjustments
) -> tuple[list[RefinedCategoryRow], Decimal, list[str]]:
    """Apply seasonal multipliers to the baseline categories.

    Returns (rows, refined_expense_total, notes).

    Notes capture any adjustments the model returned for categories
    that don't exist in the baseline (hallucinated category_id). Those
    adjustments are ignored, not applied; the note surfaces in the UI
    tooltip so the user can see what was skipped.
    """
    by_id: dict[int, "object"] = {adj.category_id: adj for adj in adjustments.seasonal}

    notes: list[str] = []
    rows: list[RefinedCategoryRow] = []
    refined_total = Decimal("0")

    for cat in baseline.get("categories", []):
        cat_id = int(cat["category_id"])
        baseline_amount = Decimal(str(cat["forecast"]))
        adj = by_id.get(cat_id)
        if adj is None:
            multiplier = 1.0
            refined_amount = baseline_amount
        else:
            multiplier = float(adj.multiplier)
            # Decimal * float via str round-trip to keep the quantization
            # deterministic and avoid float-binary representation drift.
            refined_amount = (
                baseline_amount * Decimal(str(multiplier))
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        rows.append(
            RefinedCategoryRow(
                category_id=cat_id,
                category_name=cat["category_name"],
                baseline_forecast=baseline_amount,
                multiplier=multiplier,
                refined_forecast=refined_amount,
            )
        )
        refined_total += refined_amount

    # Surface any adjustments that referenced categories not present in
    # the baseline (e.g. model hallucinated a category) so the UI can
    # flag them. We don't apply those, they contribute 0 to refined.
    baseline_ids = {int(c["category_id"]) for c in baseline.get("categories", [])}
    for adj in adjustments.seasonal:
        if adj.category_id not in baseline_ids:
            notes.append(
                f"Ignored adjustment for unknown category_id={adj.category_id} "
                f"({adj.category_name!r})"
            )

    return rows, refined_total, notes


def _baseline_response(
    *, baseline: dict, fallback_reason: str
) -> RefinedForecastResponse:
    """Build a refined-response that mirrors the baseline with no AI applied."""
    rows = [
        RefinedCategoryRow(
            category_id=int(cat["category_id"]),
            category_name=cat["category_name"],
            baseline_forecast=Decimal(str(cat["forecast"])),
            multiplier=1.0,
            refined_forecast=Decimal(str(cat["forecast"])),
        )
        for cat in baseline.get("categories", [])
    ]
    return RefinedForecastResponse(
        period_start=baseline["period_start"],
        period_end=baseline["period_end"],
        baseline_forecast_expense=Decimal(baseline["forecast_expense"]),
        refined_forecast_expense=Decimal(baseline["forecast_expense"]),
        baseline_forecast_income=Decimal(baseline["forecast_income"]),
        refined_forecast_income=Decimal(baseline["forecast_income"]),
        categories=rows,
        anomalies=[],
        provenance=RefinedForecastProvenance(
            ai_applied=False,
            fallback_reason=fallback_reason,
            model=None,
            confidence=None,
            summary=None,
            notes=[],
        ),
    )


async def _resolve_model_or_none(
    db: AsyncSession, *, org_id: int
) -> Optional[str]:
    """Return the routed model string for ROUTING_KEY, or None if not configured."""
    routing = await ai_routing_service.get_routing_for_feature(
        db, org_id=org_id, feature_name=ROUTING_KEY
    )
    if routing is None:
        return None
    _credential_id, model = routing
    return model


def _coerce_adjustments(parsed: Any) -> dict:
    """Sanitize the model's structured output into a shape the strict
    ``AIForecastAdjustments`` will accept.

    LLM output is non-deterministic: multipliers drift outside [0.5, 1.5],
    rationales run long, ``severity`` comes back as a freeform word, numbers
    arrive as strings. The OLD path validated the raw dict and fell back to
    baseline on the FIRST violation, discarding the entire refinement (the prod
    ``ai_response_invalid_schema`` failure, error_count ~= #categories). Instead
    we clamp/truncate/coerce per field and drop only individual unusable rows,
    so one stray field never nukes the whole response. Safety is preserved: the
    multiplier stays bounded to [0.5, 1.5], so the baseline math can't be blown
    out.
    """
    def _as_float(v: Any, default: float) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def _clamp(v: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, v))

    if not isinstance(parsed, dict):
        parsed = {}

    seasonal: list[dict] = []
    for row in parsed.get("seasonal") or []:
        if not isinstance(row, dict):
            continue
        try:
            cid = int(row.get("category_id"))
        except (TypeError, ValueError):
            continue  # a seasonal adjustment is useless without a category id
        name = row.get("category_name")
        name = str(name) if name is not None else ""
        if not name:
            continue
        seasonal.append(
            {
                "category_id": cid,
                "category_name": name,
                "multiplier": _clamp(_as_float(row.get("multiplier"), 1.0), 0.5, 1.5),
                "rationale": str(row.get("rationale") or "")[:240],
            }
        )

    anomalies: list[dict] = []
    for row in parsed.get("anomalies") or []:
        if not isinstance(row, dict):
            continue
        name = row.get("category_name")
        name = str(name) if name is not None else ""
        if not name:
            continue
        try:
            cid = int(row["category_id"]) if row.get("category_id") is not None else None
        except (TypeError, ValueError):
            cid = None
        severity = row.get("severity")
        if severity not in ("info", "warning", "alert"):
            severity = "info"
        anomalies.append(
            {
                "category_id": cid,
                "category_name": name,
                "description": str(row.get("description") or "")[:240],
                "severity": severity,
            }
        )

    return {
        "seasonal": seasonal,
        "anomalies": anomalies,
        "confidence": _clamp(_as_float(parsed.get("confidence"), 0.5), 0.0, 1.0),
        "summary": str(parsed.get("summary") or "")[:480],
    }


async def refine_forecast(
    db: AsyncSession,
    *,
    org_id: int,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
    period_start: Optional[datetime.date] = None,
    timeframe_months: int = 6,
    scope: Scope = Scope.TOP_20,
) -> RefinedForecastResponse:
    """Public entry point. Always returns a usable response, falling
    back to baseline on any LLM-side failure.

    All LLM/dispatch failures are swallowed and surfaced via
    ``provenance.ai_applied=False``.

    ``session_factory`` is optional only because some existing tests
    call this without one; the router always passes it. When provided,
    the LLM dispatch runs in its own session so the dispatcher's
    ledger commit can't bleed into the request transaction (same
    pattern as the categorize + budget services).
    """
    baseline = await forecast_service.compute_forecast(
        db, org_id, period_start=period_start
    )

    # Build the history + index for the prompt. The history window
    # ends STRICTLY before period_start so the forecast period's own
    # actuals don't leak into the seasonality signal.
    p_start = datetime.date.fromisoformat(baseline["period_start"])
    try:
        history = await _build_category_history(
            db, org_id=org_id, period_start=p_start, months=timeframe_months
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "ai.forecast.refine.history_failed",
            org_id=org_id,
            error_class=type(exc).__name__,
        )
        return _baseline_response(
            baseline=baseline, fallback_reason="history_build_failed"
        )

    if not history:
        # No actuals to detect seasonality from. Return baseline with
        # an explanatory fallback reason instead of paying for a call
        # that has no signal to learn from.
        return _baseline_response(
            baseline=baseline, fallback_reason="insufficient_history"
        )

    category_index = await _category_index(db, org_id=org_id)
    messages, _est_out, n_in_scope = _build_refine_prompt(
        baseline=baseline,
        history=history,
        category_index=category_index,
        timeframe_months=timeframe_months,
        scope=scope,
    )
    max_tokens = max_tokens_for_output_estimate(n_in_scope)

    # Dispatch through the cap + ledger chokepoint. Any failure here
    # (no routing, hard cap exceeded, adapter network error, retry
    # budget exhausted) becomes a fallback rather than a 5xx.
    #
    # The dispatcher commits the session it's given (via
    # _write_ledger_row); use a dedicated session when one is
    # available so the commit can't bleed into the request transaction.
    # Same pattern as categorize + budget services.
    try:
        if session_factory is not None:
            async with session_factory() as dispatch_db:
                result = await ai_dispatch.call_llm_structured(
                    dispatch_db,
                    org_id=org_id,
                    feature_key=ROUTING_KEY,
                    messages=messages,
                    response_schema=RESPONSE_JSON_SCHEMA,
                    max_tokens=max_tokens,
                )
        else:
            result = await ai_dispatch.call_llm_structured(
                db,
                org_id=org_id,
                feature_key=ROUTING_KEY,
                messages=messages,
                response_schema=RESPONSE_JSON_SCHEMA,
                max_tokens=max_tokens,
            )
    except ai_dispatch.NoRoutingConfigured:
        return _baseline_response(
            baseline=baseline, fallback_reason="ai_routing_not_configured"
        )
    except ai_dispatch.AICapExceeded:
        logger.info(
            "ai.forecast.refine.cap_exceeded",
            org_id=org_id,
        )
        return _baseline_response(
            baseline=baseline, fallback_reason="ai_cap_exceeded"
        )
    except ai_dispatch.AICapabilityNotSupported:
        return _baseline_response(
            baseline=baseline,
            fallback_reason="ai_capability_not_supported",
        )
    except NativeNotAvailable:
        # Provider doesn't natively support structured output. Per
        # ai_dispatch this is re-raised directly (NOT wrapped); the
        # other LAI services catch it in their fallback tuple, so we
        # do the same here. Without this the endpoint 5xxs.
        logger.info(
            "ai.forecast.refine.native_not_available",
            org_id=org_id,
        )
        return _baseline_response(
            baseline=baseline,
            fallback_reason="ai_native_not_available",
        )
    except StructuredOutputError:
        logger.warning(
            "ai.forecast.refine.structured_exhausted",
            org_id=org_id,
        )
        return _baseline_response(
            baseline=baseline,
            fallback_reason="ai_structured_output_failed",
        )
    except ai_dispatch.AIDispatchFailed as exc:
        logger.warning(
            "ai.forecast.refine.dispatch_failed",
            org_id=org_id,
            error_class=exc.code,
        )
        return _baseline_response(
            baseline=baseline, fallback_reason=f"ai_dispatch_failed:{exc.code}"
        )

    # Sanitize the model output (clamp multipliers, truncate text, coerce
    # types, drop unusable rows) BEFORE the strict validation, so a single
    # stray field on one row no longer discards the entire refinement. We
    # still validate the coerced dict as the final safety gate — if it
    # somehow fails we log the exact failing fields (loc + type, never the
    # values, to keep user-typed category names out of the logs) and fall
    # back, but this should now be unreachable for normal model drift.
    try:
        adjustments = AIForecastAdjustments.model_validate(
            _coerce_adjustments(result.response.parsed)
        )
    except PydanticValidationError as exc:
        logger.warning(
            "ai.forecast.refine.invalid_schema",
            org_id=org_id,
            error_count=len(exc.errors()),
            error_fields=[
                {"loc": ".".join(str(p) for p in e["loc"]), "type": e["type"]}
                for e in exc.errors()[:20]
            ],
        )
        return _baseline_response(
            baseline=baseline,
            fallback_reason="ai_response_invalid_schema",
        )

    rows, refined_expense, notes = _apply_adjustments(
        baseline=baseline, adjustments=adjustments
    )

    return RefinedForecastResponse(
        period_start=baseline["period_start"],
        period_end=baseline["period_end"],
        baseline_forecast_expense=Decimal(baseline["forecast_expense"]),
        refined_forecast_expense=refined_expense,
        baseline_forecast_income=Decimal(baseline["forecast_income"]),
        # Income side stays unmodified in this PR. The model's seasonal
        # multipliers target expense categories only. Income forecasts
        # come from recurring + executed which is a deterministic
        # signal already.
        refined_forecast_income=Decimal(baseline["forecast_income"]),
        categories=rows,
        anomalies=adjustments.anomalies,
        provenance=RefinedForecastProvenance(
            ai_applied=True,
            fallback_reason=None,
            model=result.response.model,
            confidence=adjustments.confidence,
            summary=adjustments.summary,
            notes=notes,
        ),
    )


async def estimate_refine(
    db: AsyncSession,
    *,
    org_id: int,
    period_start: Optional[datetime.date],
    timeframe_months: int,
    scope: Scope,
) -> ForecastRefineEstimate:
    """No-LLM preflight estimate for the refine endpoint.

    Computes token estimates and cost from the same prompt builder used
    by ``refine_forecast`` so the quoted cost can't drift from what runs.
    Never dispatches to an LLM.

    Returns a ``ForecastRefineEstimate`` with ``can_proceed=False`` when:
    - No transaction history exists for the org (``reason="insufficient_history"``)
    - No routing is configured (``reason="ai_routing_not_configured"``)

    KNOWN LIMITATION: checks routing but does NOT pre-check the AI spend
    cap, so an org at its hard cap can see ``can_proceed=True``. On
    Confirm the dispatch enforces the cap and returns a graceful baseline
    fallback with ``fallback_reason="ai_cap_exceeded"`` -- no overspend
    occurs. Follow-up: add a cap pre-check here.
    """
    baseline = await forecast_service.compute_forecast(
        db, org_id, period_start=period_start
    )
    p_start = datetime.date.fromisoformat(baseline["period_start"])
    history = await _build_category_history(
        db, org_id=org_id, period_start=p_start, months=timeframe_months
    )

    if not history:
        return ForecastRefineEstimate(
            est_prompt_tokens=0,
            est_output_tokens=0,
            est_cost_cents=0,
            duration_band=_duration_band(scope),
            can_proceed=False,
            reason="insufficient_history",
        )

    category_index = await _category_index(db, org_id=org_id)
    messages, est_out, n_in_scope = _build_refine_prompt(
        baseline=baseline,
        history=history,
        category_index=category_index,
        timeframe_months=timeframe_months,
        scope=scope,
    )

    prompt_text = "".join(m["content"] for m in messages)
    est_prompt = estimate_prompt_tokens(prompt_text)

    model = await _resolve_model_or_none(db, org_id=org_id)
    if model is None:
        return ForecastRefineEstimate(
            est_prompt_tokens=est_prompt,
            est_output_tokens=est_out,
            est_cost_cents=0,
            duration_band=_duration_band(scope),
            can_proceed=False,
            reason="ai_routing_not_configured",
        )

    cost = estimate_cost_cents(
        model=model, prompt_tokens=est_prompt, completion_tokens=est_out
    )
    return ForecastRefineEstimate(
        est_prompt_tokens=est_prompt,
        est_output_tokens=est_out,
        est_cost_cents=int(cost),
        duration_band=_duration_band(scope),
        can_proceed=True,
        reason=None,
    )
