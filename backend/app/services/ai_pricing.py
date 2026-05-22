"""Cost-estimate constants for AI provider models (PR2 of AI tier train).

Architect lock #18 / spec §7: cost estimates are **code constants**,
updated quarterly via manual PR. No nightly crawler, no managed price
feed. Approximate cost is fine — the cap is a guardrail, not an
accounting truth.

Updated quarterly via manual PR. No nightly crawler — see architect
lock #14 in memory and spec §7.

Values are USD cents per 1,000,000 tokens. Source for the v1 table is
each provider's public pricing page as of 2026-05-22. The ``_default``
row is a conservative high cost so an unknown model never silently
under-meters (better to refuse a legitimate call after a polite
warning than to accidentally let an org rack up unmetered spend).

``estimate_cost_cents`` rounds **up** to the nearest cent — the cap
is a ceiling, not a budget, and the rounding direction must match.

PR3 adds embedding-model rows. Embeddings only charge on the input
(``completion_per_1m_cents=0``) — feature surfaces hand
``completion_tokens=0`` to ``estimate_cost_cents``.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPricing:
    """Per-1M-token cost in USD cents.

    Both ``prompt_per_1m_cents`` and ``completion_per_1m_cents`` are
    integers in *cents* (not dollars), to match the rest of the cap /
    ledger stack which is INT-cents end to end.
    """

    prompt_per_1m_cents: int
    completion_per_1m_cents: int


# Models pinned for the v1 ledger. Add new ones in a quarterly PR;
# unknown models fall through to ``_default`` below.
#
# Pricing reference (2026-05-22, USD cents per 1M tokens):
#   gpt-4o                  : $2.50 in / $10.00 out   → 250 / 1000
#   gpt-4o-mini             : $0.15 in / $0.60 out    → 15  / 60
#   claude-sonnet-4-7       : $3.00 in / $15.00 out   → 300 / 1500
#   claude-haiku-4-5        : $0.80 in / $4.00 out    → 80  / 400
#   text-embedding-3-small  : $0.02 in (no out)       → 2   / 0
#   text-embedding-3-large  : $0.13 in (no out)       → 13  / 0
MODEL_PRICING: dict[str, ModelPricing] = {
    "gpt-4o": ModelPricing(prompt_per_1m_cents=250, completion_per_1m_cents=1000),
    "gpt-4o-mini": ModelPricing(prompt_per_1m_cents=15, completion_per_1m_cents=60),
    "claude-sonnet-4-7": ModelPricing(
        prompt_per_1m_cents=300, completion_per_1m_cents=1500
    ),
    "claude-haiku-4-5": ModelPricing(
        prompt_per_1m_cents=80, completion_per_1m_cents=400
    ),
    # Embedding models — input-only pricing. Completion column held at
    # zero so a future call site that accidentally passes
    # completion_tokens still doesn't double-bill an embedding row.
    "text-embedding-3-small": ModelPricing(
        prompt_per_1m_cents=2, completion_per_1m_cents=0
    ),
    "text-embedding-3-large": ModelPricing(
        prompt_per_1m_cents=13, completion_per_1m_cents=0
    ),
    # Conservative fallback — picked to be higher than every known
    # frontier model. Unknown-model usage gets counted at this rate so
    # the cap fires sooner rather than later. Refresh during the
    # quarterly PR if frontier prices climb past this value.
    "_default": ModelPricing(
        prompt_per_1m_cents=1500, completion_per_1m_cents=6000
    ),
}


def get_pricing(model: str) -> ModelPricing:
    """Return the pricing row for ``model``, or the ``_default`` row."""
    return MODEL_PRICING.get(model, MODEL_PRICING["_default"])


def estimate_cost_cents(
    *, model: str, prompt_tokens: int, completion_tokens: int
) -> int:
    """Compute the integer-cent cost estimate for a single call.

    Cost = (prompt_tokens * prompt_per_1m / 1_000_000)
         + (completion_tokens * completion_per_1m / 1_000_000)

    Rounded **up** to the nearest cent (math.ceil). The cap is a
    ceiling, so under-rounding would defeat the guardrail. Zero
    tokens => zero cents.
    """
    if prompt_tokens <= 0 and completion_tokens <= 0:
        return 0
    pricing = get_pricing(model)
    raw = (
        prompt_tokens * pricing.prompt_per_1m_cents
        + completion_tokens * pricing.completion_per_1m_cents
    )
    # math.ceil on a fraction; integer-only arithmetic so we don't
    # round through a float.
    cents, remainder = divmod(raw, 1_000_000)
    if remainder > 0:
        cents += 1
    return cents
