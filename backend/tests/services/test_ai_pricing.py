"""Pricing-table + cost-estimation tests (PR2 of AI tier train).

Pins:

- Each model in the v1 table returns its documented per-1M-token cost.
- Unknown models fall through to ``_default``, which is conservatively
  HIGHER than every shipped model (so unknown-model usage doesn't
  silently under-meter).
- ``estimate_cost_cents`` rounds UP to the nearest cent — the cap is
  a ceiling, not a budget.
"""
from __future__ import annotations

import pytest

from app.services.ai_pricing import (
    MODEL_PRICING,
    estimate_cost_cents,
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
        estimate_cost_cents(
            model="gpt-4o-mini", prompt_tokens=0, completion_tokens=0
        )
        == 0
    )


def test_cost_computation_known_model():
    # gpt-4o-mini: 15 cents per 1M prompt, 60 per 1M completion.
    # 1_000_000 prompt + 1_000_000 completion = 15 + 60 = 75 cents.
    cents = estimate_cost_cents(
        model="gpt-4o-mini",
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
    )
    assert cents == 75


def test_cost_rounds_up_to_nearest_cent():
    # gpt-4o-mini: 1 prompt token = 15 / 1_000_000 cent. Sub-cent
    # cost must round UP to 1, not down to 0.
    cents = estimate_cost_cents(
        model="gpt-4o-mini", prompt_tokens=1, completion_tokens=0
    )
    assert cents == 1


def test_cost_unknown_model_uses_default():
    cents_known = estimate_cost_cents(
        model="gpt-4o",
        prompt_tokens=100_000,
        completion_tokens=0,
    )
    cents_unknown = estimate_cost_cents(
        model="future-llm-x9",
        prompt_tokens=100_000,
        completion_tokens=0,
    )
    assert cents_unknown >= cents_known


def test_cost_integer_only_math_avoids_float_truncation():
    # 3 tokens at 15 / 1_000_000 = 45 / 1_000_000 < 1 cent; ceil -> 1.
    cents = estimate_cost_cents(
        model="gpt-4o-mini", prompt_tokens=3, completion_tokens=0
    )
    assert cents == 1


@pytest.mark.parametrize(
    "model,p_tokens,c_tokens,expected",
    [
        # gpt-4o (250/1000): 500_000 prompt + 500_000 completion =
        # 125 + 500 = 625 cents.
        ("gpt-4o", 500_000, 500_000, 625),
        # claude-sonnet-4-7 (300/1500): 1_000_000 + 0 = 300 cents.
        ("claude-sonnet-4-7", 1_000_000, 0, 300),
        # claude-haiku-4-5 (80/400): 0 + 1_000_000 = 400 cents.
        ("claude-haiku-4-5", 0, 1_000_000, 400),
    ],
)
def test_cost_table_values(model, p_tokens, c_tokens, expected):
    assert (
        estimate_cost_cents(
            model=model,
            prompt_tokens=p_tokens,
            completion_tokens=c_tokens,
        )
        == expected
    )
