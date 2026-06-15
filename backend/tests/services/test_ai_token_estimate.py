"""Unit tests for the shared, DB-free AI token estimator.

Covers the prompt-token char heuristic over chat ``messages`` (string
content, multimodal list content, and malformed/missing content that
must never raise), plus the per-model default output-token ceiling
used to project worst-case completion cost.
"""
from __future__ import annotations

import math

import pytest

from app.services.ai_pricing import MODEL_PRICING
from app.services.ai_token_estimate import (
    _GLOBAL_DEFAULT_MAX_OUTPUT_TOKENS,
    _PROMPT_CHARS_PER_TOKEN,
    default_max_output_tokens_for,
    estimate_prompt_tokens_from_messages,
)


def _expected_tokens(total_chars: int) -> int:
    return math.ceil(total_chars / _PROMPT_CHARS_PER_TOKEN)


# ---------- estimate_prompt_tokens_from_messages ----------------------


def test_string_content_uses_char_heuristic():
    messages = [
        {"role": "system", "content": "x" * 35},
        {"role": "user", "content": "y" * 35},
    ]
    assert estimate_prompt_tokens_from_messages(messages) == _expected_tokens(70)


def test_multimodal_list_content_sums_text_parts_only():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "a" * 20},
                {"type": "image_url", "image_url": {"url": "http://x"}},
                {"type": "text", "text": "b" * 20},
                {"type": "text"},  # malformed text part, no "text" key
                "loose-string-part-ignored",  # non-dict part
            ],
        }
    ]
    # Only the two well-formed text parts (40 chars) contribute.
    assert estimate_prompt_tokens_from_messages(messages) == _expected_tokens(40)


def test_missing_none_and_non_str_content_never_raises():
    messages = [
        {"role": "user"},  # no content key
        {"role": "user", "content": None},
        {"role": "user", "content": 12345},  # int content
        {"role": "user", "content": {"unexpected": "dict"}},  # dict content
        "not-a-dict-message",  # message is not a dict
    ]
    # None of these contribute chars; result is 0, and nothing raised.
    assert estimate_prompt_tokens_from_messages(messages) == 0


def test_empty_messages_returns_zero():
    assert estimate_prompt_tokens_from_messages([]) == 0


def test_mixed_string_and_list_content():
    messages = [
        {"role": "system", "content": "s" * 14},
        {
            "role": "user",
            "content": [{"type": "text", "text": "t" * 14}],
        },
    ]
    assert estimate_prompt_tokens_from_messages(messages) == _expected_tokens(28)


# ---------- default_max_output_tokens_for -----------------------------


@pytest.mark.parametrize("model", sorted(MODEL_PRICING.keys()))
def test_known_models_have_an_int_default(model):
    if model == "_default":
        # Not a real model id; covered by the unknown-model path.
        return
    value = default_max_output_tokens_for(model)
    assert isinstance(value, int)
    # Embedding models produce no completion tokens (0 ceiling); chat
    # models must have a positive worst-case ceiling.
    if model.startswith("text-embedding"):
        assert value == 0
    else:
        assert value > 0


def test_unknown_model_falls_back_to_global_default():
    assert (
        default_max_output_tokens_for("totally-made-up-model-xyz")
        == _GLOBAL_DEFAULT_MAX_OUTPUT_TOKENS
    )


def test_global_default_is_a_conservative_ceiling_not_context_window():
    # Sanity: a few-thousand-token ceiling, not a 100k+ context window.
    assert 1000 <= _GLOBAL_DEFAULT_MAX_OUTPUT_TOKENS <= 16000
