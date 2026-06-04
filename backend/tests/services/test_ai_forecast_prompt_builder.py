# backend/tests/services/test_ai_forecast_prompt_builder.py
import json
from app.services.ai_forecast_refine_service import _build_refine_prompt
from app.services.ai_forecast_refine_token_estimate import Scope


def _ctx():
    baseline = {
        "period_start": "2026-06-01", "period_end": "2026-06-30",
        "forecast_income": "5000", "forecast_expense": "3000",
        "categories": [
            {"category_id": 1, "category_name": "Rent", "forecast": "1500"},
            {"category_id": 2, "category_name": "Food", "forecast": "600"},
            {"category_id": 3, "category_name": "Tiny", "forecast": "5"},
        ],
    }
    history = [
        {"category_id": 1, "month": "2026-05", "total_expense": "1500"},
        {"category_id": 2, "month": "2026-05", "total_expense": "600"},
        {"category_id": 3, "month": "2026-05", "total_expense": "5"},
    ]
    index = {1: "Rent", 2: "Food", 3: "Tiny"}
    return baseline, history, index


def test_scope_top_limits_categories_and_returns_estimate():
    baseline, history, index = _ctx()
    messages, est_out, n_in_scope = _build_refine_prompt(
        baseline=baseline, history=history, category_index=index,
        timeframe_months=6, scope=Scope.TOP_20,
    )
    # all 3 fit under top_20
    user = json.loads(messages[1]["content"])
    assert {c["category_id"] for c in user["baseline_forecast"]["categories"]} == {1, 2, 3}
    assert est_out > 0
    assert n_in_scope == 3


def test_system_prompt_reflects_timeframe():
    baseline, history, index = _ctx()
    messages, _, n_in_scope = _build_refine_prompt(
        baseline=baseline, history=history, category_index=index,
        timeframe_months=12, scope=Scope.ALL,
    )
    assert "12-month" in messages[0]["content"]
    assert n_in_scope == 3


def test_top_10_drops_lowest_spend_category():
    baseline, history, index = _ctx()
    # force a tiny scope by monkeypatching is overkill; use a 11-category set:
    baseline["categories"] = [
        {"category_id": i, "category_name": f"C{i}", "forecast": str(100 - i)}
        for i in range(1, 12)
    ]
    history = [
        {"category_id": i, "month": "2026-05", "total_expense": str(100 - i)}
        for i in range(1, 12)
    ]
    index = {i: f"C{i}" for i in range(1, 12)}
    messages, _, n_in_scope = _build_refine_prompt(
        baseline=baseline, history=history, category_index=index,
        timeframe_months=6, scope=Scope.TOP_10,
    )
    user = json.loads(messages[1]["content"])
    ids = {c["category_id"] for c in user["baseline_forecast"]["categories"]}
    assert len(ids) == 10
    assert 11 not in ids  # lowest spend dropped
    assert n_in_scope == 10
