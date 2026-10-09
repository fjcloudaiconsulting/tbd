"""Cost-estimate constants for AI provider models (PR2 of AI tier train).

Architect lock #18 / spec §7: cost estimates are **code constants**,
updated quarterly via manual PR. No nightly crawler, no managed price
feed. Approximate cost is fine — the cap is a guardrail, not an
accounting truth.

Updated quarterly via manual PR. No nightly crawler — see architect
lock #14 in memory and spec §7.

Values are USD cents per 1,000,000 tokens, from each provider's public
pricing page (sources and read date beside ``MODEL_PRICING``). The ``_default``
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


# Models pinned for the ledger. Ids match EXACTLY (no prefix or date-suffix
# folding): a dated snapshot, a ``-pro`` tier or an OpenRouter ``:batch``
# variant is a different price, so an unlisted id falls to ``_default``
# (over-meters) rather than borrowing a cheaper sibling's row.
#
# Each row holds the HIGHEST rate any cited source publishes for the model:
# where a price is tiered (prompt size, modality, a dated increase) or two
# sources differ, the higher one wins, so the cap never under-meters.
#
# Sources (USD per 1M tokens, standard tier, all read 2026-10-09):
#   [A] https://platform.claude.com/docs/en/about-claude/pricing
#   [O] https://developers.openai.com/api/docs/pricing
#   [G] https://ai.google.dev/gemini-api/docs/pricing (page "Last updated
#       2026-10-09 UTC")
#   [R] https://openrouter.ai/api/v1/models (OpenRouter's own rates)
#
#   gpt-4o                  : $2.50 / $10.00  [O]  -> 250 / 1000
#   gpt-4o-mini             : $0.15 / $0.60   [O]  -> 15 / 60
#   The gpt-6 / gpt-5.6 rows take the >=272K-token-prompt rate. [O] lists
#   that tier for gpt-5.6-sol only; [R] lists it for the rest.
#   gpt-6-astra             : $20 / $75 [R] ($10 / $50 [O])     -> 2000 / 7500
#   gpt-6.1-sol, gpt-6-sol  : $4 / $15 [R] ($2 / $10 [O])       -> 400 / 1500
#   gpt-6-luna              : $0.20 / $0.75 [R] ($0.10 / $0.50) -> 20 / 75
#   gpt-5.6-sol             : $8 / $30 [O] ($4 / $20 below)     -> 800 / 3000
#   gpt-5.6-terra           : $4 / $18 [R] ($2 / $12 [O])       -> 400 / 1800
#   gpt-5.6-luna            : $0.40 / $1.80 [R] ($0.20 / $1.20) -> 40 / 180
#   claude-fable-5-1, -5    : $10 / $50       [A]  -> 1000 / 5000
#   claude-opus-5-5         : $4 / $20        [A]  -> 400 / 2000
#   claude-opus-5, 4-8/7/6  : $5 / $25        [A]  -> 500 / 2500
#   claude-sonnet-5-5, -5   : $2 / $10        [A]  -> 200 / 1000
#   claude-sonnet-4-6       : $3 / $15        [A]  -> 300 / 1500
#   claude-sonnet-4-7       : $3 / $15, NOT on [A]'s model list; the
#                             2026-05-22 row stays while the platform
#                             allowlist names it
#   claude-haiku-5-5        : $0.50 / $2.50 (>100K-token prompt; $0.10 /
#                             $0.50 below)    [A]  -> 50 / 250
#   claude-haiku-4-5        : $1 / $5         [A]  -> 100 / 500
#   gemini-3.8-flash, 3.6   : $1.50 / $7.50 from 2027-01-01 ($0.75 / $3.75
#                             before)         [G]  -> 150 / 750
#   gemini-3.5-flash        : $1.50 / $9.00   [G]  -> 150 / 900
#   gemini-3.5-flash-lite   : $0.30 / $2.50   [G]  -> 30 / 250
#   gemini-3.1-pro-preview  : $4 / $18 (>200K-token prompt; $2 / $12 below)
#                                             [G]  -> 400 / 1800
#   gemini-3.1-flash-lite   : $0.50 audio-in / $1.50 ($0.25 text-in)
#                                             [G]  -> 50 / 150
#   text-embedding-3-small  : $0.02 in        [O]  -> 2 / 0
#   text-embedding-3-large  : $0.13 in        [O]  -> 13 / 0
#
# OpenRouter ids (``OPENROUTER_IDS`` below) reuse the first-party row; every
# one's [R] rate, long-prompt tier included, was at or below it on 2026-10-09.
MODEL_PRICING: dict[str, ModelPricing] = {
    "gpt-4o": ModelPricing(prompt_per_1m_cents=250, completion_per_1m_cents=1000),
    "gpt-4o-mini": ModelPricing(prompt_per_1m_cents=15, completion_per_1m_cents=60),
    "gpt-6-astra": ModelPricing(2000, 7500),
    "gpt-6.1-sol": ModelPricing(400, 1500),
    "gpt-6-sol": ModelPricing(400, 1500),
    "gpt-6-luna": ModelPricing(20, 75),
    "gpt-5.6-sol": ModelPricing(800, 3000),
    "gpt-5.6-terra": ModelPricing(400, 1800),
    "gpt-5.6-luna": ModelPricing(40, 180),
    "claude-fable-5-1": ModelPricing(1000, 5000),
    "claude-fable-5": ModelPricing(1000, 5000),
    "claude-opus-5-5": ModelPricing(400, 2000),
    "claude-opus-5": ModelPricing(500, 2500),
    "claude-opus-4-8": ModelPricing(500, 2500),
    "claude-opus-4-7": ModelPricing(500, 2500),
    "claude-opus-4-6": ModelPricing(500, 2500),
    "claude-sonnet-5-5": ModelPricing(200, 1000),
    "claude-sonnet-5": ModelPricing(200, 1000),
    "claude-sonnet-4-6": ModelPricing(300, 1500),
    "claude-sonnet-4-7": ModelPricing(
        prompt_per_1m_cents=300, completion_per_1m_cents=1500
    ),
    "claude-haiku-5-5": ModelPricing(50, 250),
    "claude-haiku-4-5": ModelPricing(100, 500),
    # The dated id Anthropic's /v1/models lists for Haiku 4.5.
    "claude-haiku-4-5-20251001": ModelPricing(100, 500),
    "gemini-3.8-flash": ModelPricing(150, 750),
    "gemini-3.6-flash": ModelPricing(150, 750),
    "gemini-3.5-flash": ModelPricing(150, 900),
    "gemini-3.5-flash-lite": ModelPricing(30, 250),
    "gemini-3.1-pro-preview": ModelPricing(400, 1800),
    "gemini-3.1-flash-lite": ModelPricing(50, 150),
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
    # quarterly PR if frontier prices climb past this value. Raised from
    # 1500 / 6000 in TBD-618: gpt-6-astra's long-prompt rate passed it.
    "_default": ModelPricing(
        prompt_per_1m_cents=2500, completion_per_1m_cents=10000
    ),
}

# OpenRouter id -> the first-party id whose row it shares.
OPENROUTER_IDS: dict[str, str] = {
    "anthropic/claude-fable-5.1": "claude-fable-5-1",
    "anthropic/claude-fable-5": "claude-fable-5",
    "anthropic/claude-opus-5.5": "claude-opus-5-5",
    "anthropic/claude-opus-5": "claude-opus-5",
    "anthropic/claude-opus-4.8": "claude-opus-4-8",
    "anthropic/claude-opus-4.7": "claude-opus-4-7",
    "anthropic/claude-opus-4.6": "claude-opus-4-6",
    "anthropic/claude-sonnet-5.5": "claude-sonnet-5-5",
    "anthropic/claude-sonnet-5": "claude-sonnet-5",
    "anthropic/claude-sonnet-4.6": "claude-sonnet-4-6",
    "anthropic/claude-haiku-5.5": "claude-haiku-5-5",
    "anthropic/claude-haiku-4.5": "claude-haiku-4-5",
    "openai/gpt-6-astra": "gpt-6-astra",
    "openai/gpt-6.1-sol": "gpt-6.1-sol",
    "openai/gpt-6-sol": "gpt-6-sol",
    "openai/gpt-6-luna": "gpt-6-luna",
    "openai/gpt-5.6-sol": "gpt-5.6-sol",
    "openai/gpt-5.6-terra": "gpt-5.6-terra",
    "openai/gpt-5.6-luna": "gpt-5.6-luna",
    "google/gemini-3.8-flash": "gemini-3.8-flash",
    "google/gemini-3.6-flash": "gemini-3.6-flash",
    "google/gemini-3.5-flash": "gemini-3.5-flash",
    "google/gemini-3.5-flash-lite": "gemini-3.5-flash-lite",
    "google/gemini-3.1-pro-preview": "gemini-3.1-pro-preview",
    "google/gemini-3.1-flash-lite": "gemini-3.1-flash-lite",
}
MODEL_PRICING.update({k: MODEL_PRICING[v] for k, v in OPENROUTER_IDS.items()})


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
