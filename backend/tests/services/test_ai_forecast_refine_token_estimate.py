# backend/tests/services/test_ai_forecast_refine_token_estimate.py
import pytest
from app.services.ai_forecast_refine_token_estimate import (
    Scope,
    select_categories_by_scope,
    estimate_prompt_tokens,
    estimate_output_tokens,
    max_tokens_for_output_estimate,
)


def test_select_top_n_by_spend_keeps_highest_only():
    # spend_by_cat: {category_id: total_spend}
    spend = {1: 100.0, 2: 5000.0, 3: 50.0, 4: 900.0}
    assert select_categories_by_scope(spend, Scope.TOP_10) == [2, 4, 1, 3]
    assert select_categories_by_scope(spend, Scope.ALL) == [2, 4, 1, 3]
    # top_n truncates; with a tiny n via TOP_10 on 4 items we keep all 4
    top2 = select_categories_by_scope({1: 10.0, 2: 20.0, 3: 30.0}, Scope.TOP_10)
    assert top2 == [3, 2, 1]


def test_estimate_output_tokens_grows_with_category_count():
    few = estimate_output_tokens(category_count=5)
    many = estimate_output_tokens(category_count=40)
    assert many > few
    assert few > 0


def test_max_tokens_never_below_floor_and_covers_estimate():
    # floor is 1024 (today's default); sizing adds headroom above the estimate
    assert max_tokens_for_output_estimate(10) >= 1024
    big = estimate_output_tokens(category_count=40)
    assert max_tokens_for_output_estimate(40) > big


def test_estimate_prompt_tokens_is_char_based():
    short = estimate_prompt_tokens("x" * 350)
    assert short == pytest.approx(100, abs=5)  # ~1 token / 3.5 chars
