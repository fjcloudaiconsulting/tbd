"""Canonical mapping for the 3 user-facing AI features.

The entitlement key (feature catalog), the routing feature name (dispatch),
and the UI id all differ. This is the ONE place that triple lives; any drift
is a bug (guarded by tests/services/test_ai_feature_map.py).
"""
from __future__ import annotations

# (entitlement_key, routing_name, ui_id)
AI_FEATURE_MAP: tuple[tuple[str, str, str], ...] = (
    ("ai.autocategorize", "categorize_transactions", "categorize"),
    ("ai.forecast", "smart_forecast", "forecast"),
    ("ai.budget", "smart_budget", "budget"),
)


def ui_to_routing(ui_id: str) -> str:
    for _ent, routing, ui in AI_FEATURE_MAP:
        if ui == ui_id:
            return routing
    raise KeyError(ui_id)
