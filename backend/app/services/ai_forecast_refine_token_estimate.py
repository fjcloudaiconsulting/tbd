# backend/app/services/ai_forecast_refine_token_estimate.py
"""Pure (DB-free) helpers for the cost-confirmed forecast-refine flow.

Kept separate from the service so the heuristic + scope selection are
unit-testable without a database or LLM. The SAME functions back both
the /estimate preflight and the real refine call, so the quoted cost
can't drift from what actually runs.
"""
from __future__ import annotations

import enum
import math

# ``_PROMPT_CHARS_PER_TOKEN`` is owned by ``ai_token_estimate`` and
# re-exported here so the refine preflight and the universal dispatch
# overspend gate share one char-per-token heuristic and cannot drift.
from app.services.ai_token_estimate import _PROMPT_CHARS_PER_TOKEN

# Char-per-token heuristics. The stack has no tokenizer; these are
# deliberately rough and surfaced to the user as approximate ("≈").
_OUTPUT_CHARS_PER_TOKEN = 3.0

# Per-row JSON size assumptions for the output shape (SeasonalAdjustment,
# AnomalyFlag) plus fixed overhead (confidence, summary, braces).
_SEASONAL_CHARS_PER_ROW = 220
_ANOMALY_CHARS_PER_ROW = 180
_FIXED_OUTPUT_CHARS = 600
_OUTPUT_SAFETY_MARGIN = 1.10

# max_tokens sizing: never below today's adapter default; add headroom
# above the estimate so the tool-use JSON can't truncate before the
# required `anomalies` key (the prod bug).
_MAX_TOKENS_FLOOR = 1024
_MAX_TOKENS_BUFFER = 400

# Hard ceiling for Scope.ALL — must stay <= AIForecastAdjustments.seasonal
# max_length (200) so Pydantic validation never rejects the category list.
# At 200 categories, anomalies = 200//4 = 50, which is under the anomalies
# max_length of 60, so the constraint is satisfied at this exact ceiling.
_ALL_SCOPE_CEILING = 200  # must stay <= AIForecastAdjustments.seasonal max_length (anomalies = N//4 stays < its 60 cap at 200)


class Scope(str, enum.Enum):
    TOP_10 = "top_10"
    TOP_20 = "top_20"
    ALL = "all"


def _limit_for_scope(scope: Scope) -> int:
    match scope:
        case Scope.TOP_10:
            return 10
        case Scope.TOP_20:
            return 20
        case Scope.ALL:
            return _ALL_SCOPE_CEILING
        case _:
            raise ValueError(f"Unknown scope: {scope!r}")


def select_categories_by_scope(
    spend_by_category: dict[int, float], scope: Scope
) -> list[int]:
    """Return category ids ordered by spend desc, truncated to the scope.

    Ties broken by category_id asc for determinism.
    """
    ordered = sorted(
        spend_by_category.keys(),
        key=lambda cid: (-(spend_by_category[cid] or 0.0), cid),
    )
    return ordered[: _limit_for_scope(scope)]


def estimate_prompt_tokens(prompt_text: str) -> int:
    return math.ceil(len(prompt_text) / _PROMPT_CHARS_PER_TOKEN)


def estimate_output_tokens(*, category_count: int) -> int:
    if category_count < 0:
        raise ValueError(f"category_count must be >= 0, got {category_count}")
    anomalies = category_count // 4
    chars = (
        category_count * _SEASONAL_CHARS_PER_ROW
        + anomalies * _ANOMALY_CHARS_PER_ROW
        + _FIXED_OUTPUT_CHARS
    )
    tokens = math.ceil(chars / _OUTPUT_CHARS_PER_TOKEN)
    return math.ceil(tokens * _OUTPUT_SAFETY_MARGIN)


def max_tokens_for_output_estimate(category_count: int) -> int:
    est = estimate_output_tokens(category_count=category_count)
    return max(_MAX_TOKENS_FLOOR, est + _MAX_TOKENS_BUFFER)


def _duration_band(scope: Scope) -> str:
    return {
        Scope.TOP_10: "~15-25s",
        Scope.TOP_20: "~20-40s",
        Scope.ALL: "may take 60s+",
    }[scope]
