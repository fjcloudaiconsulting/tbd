"""Shared, DB-free token estimators for the AI dispatch overspend gate.

This module is the single home for the char-per-token prompt heuristic
and the per-model worst-case output-token ceiling. Both the universal
dispatch projected-overspend gate (``ai_dispatch._projected_cost_cents``)
and the forecast-refine UI preflight
(``ai_forecast_refine_token_estimate``) source the prompt heuristic from
here so the two estimates cannot drift apart.

Pure: no DB, no LLM, no IO. Never raises on malformed message shapes —
projection runs on the hot dispatch path and must degrade, never crash.
"""
from __future__ import annotations

import math

from app.services.ai_pricing import MODEL_PRICING

# Char-per-token heuristic. The stack has no tokenizer; this is
# deliberately rough. Shared with ``ai_forecast_refine_token_estimate``
# (which re-exports it) so the refine preflight and the dispatch gate
# can never diverge.
_PROMPT_CHARS_PER_TOKEN = 3.5


# Conservative worst-case output ceilings (in tokens) used when a caller
# does not pin ``max_tokens``. These are NOT context windows; they are a
# few-thousand-token "how big could the completion plausibly get" guess,
# matched to each model's pricing-table key. The projected cost is a
# guardrail, so over-estimating output is the safe direction (operator
# chose the "prevent overspend" posture).
_GLOBAL_DEFAULT_MAX_OUTPUT_TOKENS = 4096

_DEFAULT_MAX_OUTPUT_TOKENS_BY_MODEL: dict[str, int] = {
    "gpt-4o": 4096,
    "gpt-4o-mini": 4096,
    "claude-sonnet-4-7": 8192,
    "claude-haiku-4-5": 8192,
    # Embedding models never produce completion tokens; keep a token
    # floor so projection arithmetic stays defined (embedding pricing
    # zeroes the completion column anyway).
    "text-embedding-3-small": 0,
    "text-embedding-3-large": 0,
}


def estimate_prompt_tokens_from_messages(messages: list[dict]) -> int:
    """Estimate prompt tokens from chat ``messages`` via the char heuristic.

    Defensive by design — message/content shapes from feature surfaces
    vary and projection must never raise on the hot path:

    - ``content`` is a ``str``: count its length.
    - ``content`` is a ``list`` (multimodal parts): sum ``len(part["text"])``
      for dict parts that carry a string ``"text"`` key; ignore image
      parts, non-dict parts, and dict parts without a string ``text``.
    - ``content`` missing / ``None`` / any other type: contributes 0.
    - a message that is not a dict: contributes 0.

    Total chars are divided by ``_PROMPT_CHARS_PER_TOKEN`` and rounded up.
    """
    total_chars = 0
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if isinstance(text, str):
                    total_chars += len(text)
        # Any other content type (None, int, dict, ...) contributes 0.
    return math.ceil(total_chars / _PROMPT_CHARS_PER_TOKEN)


def default_max_output_tokens_for(model: str) -> int:
    """Worst-case completion-token ceiling for ``model``.

    Returns the per-model ceiling when known (keys mirror
    ``ai_pricing.MODEL_PRICING``), otherwise the conservative global
    fallback. Used to project completion cost when the caller does not
    pin ``max_tokens``.
    """
    return _DEFAULT_MAX_OUTPUT_TOKENS_BY_MODEL.get(
        model, _GLOBAL_DEFAULT_MAX_OUTPUT_TOKENS
    )


# Keep the per-model ceiling map honest against the pricing table: every
# known pricing model (except the synthetic ``_default`` row) should have
# an explicit output ceiling so a newly priced model is never silently
# projected at only the global fallback. This is a cheap import-time
# guard, not a runtime cost.
_missing = {
    m
    for m in MODEL_PRICING
    if m != "_default" and m not in _DEFAULT_MAX_OUTPUT_TOKENS_BY_MODEL
}
if _missing:  # pragma: no cover - defensive import-time consistency check
    raise RuntimeError(
        "ai_token_estimate: models priced but missing an output ceiling: "
        f"{sorted(_missing)}"
    )
