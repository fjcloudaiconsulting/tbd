"""Pydantic shapes for LAI.2 — Smart Forecast refinement.

The refinement endpoint layers AI-detected seasonality + anomaly flags
on top of the deterministic forecast from ``forecast_service``. The
LLM's output is validated against ``AIForecastAdjustments`` BEFORE we
apply anything to the baseline; malformed responses are rejected and
the endpoint falls back to the unmodified baseline (with a flag so the
UI can surface the fallback).

The exposed response carries both the baseline AND the refined view so
the frontend can render a side-by-side delta or the toggle's
``before/after`` tooltip without a second request.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field, StrictBool, StrictFloat, StrictInt, StrictStr


class SeasonalAdjustment(BaseModel):
    """A single seasonal multiplier applied to a category for the period.

    ``multiplier`` is a float in [0.5, 1.5] — we cap the range so a
    misbehaving model cannot wipe out (multiplier=0) or 10x
    (multiplier=10) a forecast line. Anything outside the band is
    rejected at validate time and a fallback to the baseline is
    served.
    """

    category_id: StrictInt
    category_name: StrictStr
    multiplier: StrictFloat = Field(..., ge=0.5, le=1.5)
    rationale: StrictStr = Field(..., max_length=240)


class AnomalyFlag(BaseModel):
    """An anomaly the model spotted in the recent actuals.

    Anomalies are aggregate patterns (category-level spikes, recurring
    misses) — we never echo raw transaction text back to the user.
    """

    category_id: Optional[StrictInt] = None
    category_name: StrictStr
    description: StrictStr = Field(..., max_length=240)
    severity: StrictStr = Field(..., pattern=r"^(info|warning|alert)$")


class AIForecastAdjustments(BaseModel):
    """Validated LLM response shape.

    Rejecting malformed adjustments here is the primary safety control;
    we never pass an LLM dict straight through to the baseline math.
    """

    seasonal: list[SeasonalAdjustment] = Field(default_factory=list, max_length=200)
    anomalies: list[AnomalyFlag] = Field(default_factory=list, max_length=60)
    confidence: StrictFloat = Field(..., ge=0.0, le=1.0)
    summary: StrictStr = Field(..., max_length=480)


class RefinedCategoryRow(BaseModel):
    """One category row in the refined forecast output.

    Mirrors the baseline category row but adds the multiplier that was
    applied and the resulting refined-forecast amount. The original
    baseline values stay populated so the UI can render a "was, now"
    delta inline.
    """

    category_id: StrictInt
    category_name: StrictStr
    baseline_forecast: Decimal
    multiplier: StrictFloat
    refined_forecast: Decimal


class RefinedForecastProvenance(BaseModel):
    """Where the refined numbers came from. Surfaces in the UI tooltip.

    ``ai_applied`` is False when we fell back (LLM failure, feature gate,
    invalid response). When False the refined_* values equal the
    baseline_* values and ``adjustments`` is empty.
    """

    ai_applied: StrictBool
    fallback_reason: Optional[StrictStr] = None
    model: Optional[StrictStr] = None
    confidence: Optional[StrictFloat] = None
    summary: Optional[StrictStr] = None
    notes: list[StrictStr] = Field(default_factory=list)


class RefinedForecastResponse(BaseModel):
    """Response shape for POST /api/v1/ai/forecast/refine."""

    period_start: StrictStr
    period_end: StrictStr
    baseline_forecast_expense: Decimal
    refined_forecast_expense: Decimal
    baseline_forecast_income: Decimal
    refined_forecast_income: Decimal
    categories: list[RefinedCategoryRow]
    anomalies: list[AnomalyFlag]
    provenance: RefinedForecastProvenance


class RefineForecastRequest(BaseModel):
    """Request body for the refine + estimate endpoints.

    ``period_start`` optional (defaults to the current billing period).
    ``timeframe_months`` selects history depth; ``scope`` selects how many
    categories (by spend) are refined.
    """

    period_start: Optional[StrictStr] = None
    timeframe_months: Literal[3, 6, 12] = 6
    scope: Literal["top_10", "top_20", "all"] = "top_20"


class ForecastRefineEstimate(BaseModel):
    """No-LLM preflight estimate shown before the user confirms a refine."""

    est_prompt_tokens: StrictInt
    est_output_tokens: StrictInt
    est_cost_cents: StrictInt
    duration_band: StrictStr
    can_proceed: StrictBool
    reason: Optional[StrictStr] = None
