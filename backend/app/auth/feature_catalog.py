"""L4.11 — Feature catalog and canonical PlanFeatures model.

The catalog invariant is "actively gated OR reserved by a locked
near-term roadmap dependency." `ai.autocategorize` qualifies via LAI.1.
Adding a key is a one-line edit here + a one-line edit in
frontend/lib/feature-catalog.ts; the drift-guard test pins parity.
"""
from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, StrictBool


FeatureKey = Literal[
    "ai.budget",
    "ai.forecast",
    "ai.smart_plan",
    "ai.autocategorize",
    "ai.agent",
]

ALL_FEATURE_KEYS: frozenset[str] = frozenset(get_args(FeatureKey))

# TBD-559: a MODULE is what the operator sells: catalog metadata grouping
# feature keys and usage meters. Every key and every meter sits in exactly one
# module (fenced in tests/auth/test_feature_modules.py, mirrored in
# frontend/lib/feature-catalog.ts through the generated fixture). Storage stays
# per key, so a plan can grant part of a module.
FEATURE_MODULES: dict[str, tuple[str, ...]] = {
    "ai": ("ai.agent", "ai.autocategorize", "ai.budget", "ai.forecast", "ai.smart_plan"),
}

# Meter name -> module. The meters are counted and limited by the entitlement
# resolver work that follows TBD-559; the names are a stored contract.
METER_MODULES: dict[str, str] = {
    "assistant.turns": "ai",
    "mcp.calls": "ai",
    "platform_ai.tokens": "ai",
    "platform_ai.cents": "ai",
}


class PlanFeatures(BaseModel):
    """Canonical persisted shape of plans.features.

    Every plan write canonicalizes through this model so storage
    always contains the full closed set of keys with strict-bool values.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    ai_budget:         StrictBool = Field(default=False, alias="ai.budget")
    ai_forecast:       StrictBool = Field(default=False, alias="ai.forecast")
    ai_smart_plan:     StrictBool = Field(default=False, alias="ai.smart_plan")
    ai_autocategorize: StrictBool = Field(default=False, alias="ai.autocategorize")
    ai_agent:          StrictBool = Field(default=False, alias="ai.agent")
