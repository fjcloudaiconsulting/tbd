"""Pricing-table + cost-estimation tests (PR2 of AI tier train).

Pins:

- Each model in the v1 table returns its documented per-1M-token cost.
- Unknown models fall through to ``_default``, which is conservatively
  HIGHER than every shipped model (so unknown-model usage doesn't
  silently under-meter).
- ``estimate_cost_micros`` rounds UP to the nearest micro-USD — the cap is
  a ceiling, not a budget.
"""
from __future__ import annotations

import pytest

from app.services.ai_pricing import (
    MODEL_PRICING,
    OPENROUTER_IDS,
    cost_tier,
    estimate_cost_micros,
    get_pricing,
)


def test_known_models_have_pricing():
    for model in ("gpt-4o", "gpt-4o-mini", "claude-sonnet-4-7", "claude-haiku-4-5"):
        p = get_pricing(model)
        assert p.prompt_per_1m_cents > 0
        assert p.completion_per_1m_cents > 0


def test_unknown_model_falls_back_to_default():
    p = get_pricing("future-unreleased-llm-v9")
    default = MODEL_PRICING["_default"]
    assert p is default


def test_default_is_more_expensive_than_known_models():
    """Conservative-high fallback invariant.

    If the default pricing isn't strictly higher than every known
    frontier model, unknown-model traffic will silently under-meter
    and the cap will fire late. The default IS the safety net.
    """
    default = MODEL_PRICING["_default"]
    for name, pricing in MODEL_PRICING.items():
        if name == "_default":
            continue
        assert default.prompt_per_1m_cents >= pricing.prompt_per_1m_cents, name
        assert (
            default.completion_per_1m_cents >= pricing.completion_per_1m_cents
        ), name


def test_zero_tokens_zero_cost():
    assert (
        estimate_cost_micros(
            model="gpt-4o-mini", prompt_tokens=0, completion_tokens=0
        )
        == 0
    )


def test_cost_computation_known_model():
    # gpt-4o-mini: 15 cents per 1M prompt, 60 per 1M completion.
    # 1_000_000 prompt + 1_000_000 completion = 15 + 60 = 75 cents.
    cents = estimate_cost_micros(
        model="gpt-4o-mini",
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
    )
    assert cents == 750_000


def test_cost_rounds_up_to_nearest_micro():
    # gpt-4o-mini: 1 prompt token = 0.15 micro-USD. Must round UP to 1.
    cents = estimate_cost_micros(
        model="gpt-4o-mini", prompt_tokens=1, completion_tokens=0
    )
    assert cents == 1


def test_cost_unknown_model_uses_default():
    cents_known = estimate_cost_micros(
        model="gpt-4o",
        prompt_tokens=100_000,
        completion_tokens=0,
    )
    cents_unknown = estimate_cost_micros(
        model="future-llm-x9",
        prompt_tokens=100_000,
        completion_tokens=0,
    )
    assert cents_unknown >= cents_known


def test_cost_integer_only_math_avoids_float_truncation():
    # 3 tokens at 0.15 micro each = 0.45 micro; ceil -> 1.
    cents = estimate_cost_micros(
        model="gpt-4o-mini", prompt_tokens=3, completion_tokens=0
    )
    assert cents == 1


@pytest.mark.parametrize(
    "model,p_tokens,c_tokens,expected",
    [
        # gpt-4o (250/1000): 500_000 prompt + 500_000 completion =
        # 125 + 500 = 625 cents.
        ("gpt-4o", 500_000, 500_000, 6_250_000),
        # claude-sonnet-4-7 (300/1500): 1_000_000 + 0 = 300 cents.
        ("claude-sonnet-4-7", 1_000_000, 0, 3_000_000),
        # claude-haiku-4-5 (100/500, TBD-618 correction): 0 + 1_000_000 = 500.
        ("claude-haiku-4-5", 0, 1_000_000, 5_000_000),
        # TBD-618: the current Claude models at their verified prices.
        ("claude-sonnet-5-5", 1_000_000, 1_000_000, (200 + 1000) * 10_000),
        ("claude-opus-5-5", 1_000_000, 1_000_000, (400 + 2000) * 10_000),
        ("claude-fable-5-1", 1_000_000, 1_000_000, (1000 + 5000) * 10_000),
        # Haiku 5.5 is priced by prompt size; the table holds the higher
        # (>100K-token prompt) rate so a long prompt never under-meters.
        ("claude-haiku-5-5", 1_000_000, 1_000_000, (50 + 250) * 10_000),
        # One OpenAI and Gemini row per family, at their highest rate.
        ("gpt-6-astra", 1_000_000, 1_000_000, (2000 + 7500) * 10_000),
        ("gpt-5.6-sol", 1_000_000, 1_000_000, (800 + 3000) * 10_000),
        ("gemini-3.1-pro-preview", 1_000_000, 1_000_000, (400 + 1800) * 10_000),
        ("gemini-3.8-flash", 1_000_000, 1_000_000, (150 + 750) * 10_000),
        # OpenRouter spells the same model with dots and a vendor prefix.
        ("anthropic/claude-sonnet-5.5", 1_000_000, 1_000_000, (200 + 1000) * 10_000),
    ],
)
def test_cost_table_values(model, p_tokens, c_tokens, expected):
    assert (
        estimate_cost_micros(
            model=model,
            prompt_tokens=p_tokens,
            completion_tokens=c_tokens,
        )
        == expected
    )


def test_tbd618_demo_ledger_cost_for_claude_sonnet_5_5():
    """The 2026-10-09 demo turn: 62,342 in / 1,898 out on claude-sonnet-5-5
    ($2 / $10 per 1M) costs 14,366,400 / 100 = 143,664 micro-USD ($0.1437), not the
    105 cents the old 1500 / 6000 ``_default`` row charged."""
    cents = estimate_cost_micros(
        model="claude-sonnet-5-5", prompt_tokens=62_342, completion_tokens=1_898
    )
    assert cents == 143_664


@pytest.mark.parametrize(
    "model,cheaper_sibling",
    [
        ("claude-sonnet-5-5-20260101", "claude-sonnet-5-5"),
        ("gpt-6-sol-pro", "gpt-6-sol"),
        ("anthropic/claude-sonnet-5.5:batch", "anthropic/claude-sonnet-5.5"),
    ],
)
def test_ids_match_exactly_never_by_prefix(model, cheaper_sibling):
    """A suffixed id (dated snapshot, ``-pro`` tier, OpenRouter variant) is a
    different price. It must fall to ``_default``, never borrow the row of a
    cheaper id it happens to start with."""
    assert cheaper_sibling in MODEL_PRICING
    assert get_pricing(model) is MODEL_PRICING["_default"]


def test_openrouter_ids_share_the_row_of_the_same_model():
    """OpenRouter names Anthropic models with dots (``claude-sonnet-5.5``) and
    OpenAI/Google models as-is, behind a vendor prefix. Each alias must point
    at that same model's first-party row, not a sibling with another price."""
    assert OPENROUTER_IDS
    for or_id, first_party in OPENROUTER_IDS.items():
        vendor, name = or_id.split("/")
        assert vendor in ("anthropic", "openai", "google"), or_id
        assert first_party == (
            name.replace(".", "-") if vendor == "anthropic" else name
        ), or_id
        assert MODEL_PRICING[or_id] is MODEL_PRICING[first_party], or_id


@pytest.mark.parametrize("prompt,completion,want", [
    (0, 1_999_999, "budget"),  # 3:1 blend is $0.49999975; a plain average would say standard
    (500_000, 500_000, "standard"),
    (5_000_000, 5_000_000, "standard"),
    (5_000_000, 5_000_001, "frontier"),
])
def test_cost_tier_boundaries(prompt, completion, want):
    assert cost_tier(prompt, completion) == want


def test_every_openrouter_id_has_a_label():
    for or_id in OPENROUTER_IDS:
        assert MODEL_PRICING[or_id].label, or_id


# ---------------------------------------------------------------- TBD-630


def test_fence_jira_sub_cent_calls_do_not_round_up_per_call(monkeypatch):
    """FENCE (Jira): a $0.10/M model on a 500-token prompt costs 50 micro-USD
    per call; 1,000 calls are $0.05. Kills per-call cent rounding (10,000,000)
    and float math."""
    from app.services import ai_pricing
    from app.services.ai_pricing import ModelPricing

    monkeypatch.setitem(
        ai_pricing.MODEL_PRICING, "tbd630-cheap", ModelPricing(10, 40)
    )
    total = sum(
        estimate_cost_micros(
            model="tbd630-cheap", prompt_tokens=500, completion_tokens=0
        )
        for _ in range(1000)
    )
    assert total == 50_000


def test_fence_exact_micros_and_round_up(monkeypatch):
    """FENCE: gpt-4o-mini 1,500/300 is exactly 405 micro-USD; one token at 1
    cent/Mtok is 0.01 micro and rounds UP to 1. Kills the cents path (10,000)
    and floor / round-half (0)."""
    from app.services import ai_pricing
    from app.services.ai_pricing import ModelPricing

    assert (
        estimate_cost_micros(
            model="gpt-4o-mini", prompt_tokens=1_500, completion_tokens=300
        )
        == 405
    )
    monkeypatch.setitem(ai_pricing.MODEL_PRICING, "tbd630-one", ModelPricing(1, 1))
    assert (
        estimate_cost_micros(
            model="tbd630-one", prompt_tokens=1, completion_tokens=0
        )
        == 1
    )


@pytest.mark.parametrize(
    "value,want",
    [
        (0.000123, 123),
        ("0.0000001", 1),
        (0, 0),
        (10000, 10_000_000_000),
        ("10000.000001", None),
        (1e300, None),
        (-0.001, None),
        ("NaN", None),
        (float("inf"), None),
        (True, None),
        (None, None),
        ("abc", None),
        ({}, None),
    ],
)
def test_fence_usd_to_micros(value, want):
    """FENCE: float math (0.000123 -> 124), unguarded ceil on non-finite, and
    no upper bound (1e300 would overflow BIGINT at the counter)."""
    from app.services.ai_pricing import usd_to_micros

    assert usd_to_micros(value) == want
