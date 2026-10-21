"""Canonical mapping for the user-facing AI features.

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
    ("ai.smart_plan", "smart_plan", "plan"),
    # TBD-559: one key for both agent surfaces (in-app assistant and MCP).
    ("ai.agent", "chat", "agent"),
)

CHAT_FEATURE = "chat"
# Capability each routing name's production dispatch uses (agent_chat -> call_llm_function;
# the rest -> call_llm_structured). The model list, the routing-write gate and dispatch share it.
FEATURE_CAPABILITY: dict[str, str] = {
    "chat": "function_call",
    "categorize_transactions": "structured_output",
    "smart_forecast": "structured_output",
    "smart_budget": "structured_output",
    "smart_plan": "structured_output",
}


def ui_to_routing(ui_id: str) -> str:
    for _ent, routing, ui in AI_FEATURE_MAP:
        if ui == ui_id:
            return routing
    raise KeyError(ui_id)
