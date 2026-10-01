"""L4.11 — Feature catalog and canonical PlanFeatures model.

The catalog invariant is "actively gated OR reserved by a locked
near-term roadmap dependency." `ai.autocategorize` qualifies via LAI.1.
Adding a key means: the Literal and PlanFeatures here, a FEATURE_MODULES
entry, FEATURE_LABELS / FeatureKey / PlanFeatures in the frontend, the
system plans page defaults, AI_FEATURE_MAP for an AI key, and a regenerated
frontend/tests/fixtures/feature-catalog.json (scripts/regen_feature_catalog_fixture.py).
tests/test_feature_catalog_frontend_contract.py pins the parity.

Adding a usage METER means (TBD-585): the MeterKey Literal, a METER_MODULES
entry, an aliased PlanUsageLimits field with its default, the literal default
dict in a new migration that backfills plans.usage_limits (existing rows keep
the old canonical shape until then; the read path fills the default), an
admission call at the surface it meters (app.services.usage_service.admit),
and the regenerated fixture. A ``platform_ai.*`` meter spends platform money:
its default is 0 and it can never be unlimited.
"""
from __future__ import annotations

from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator


FeatureKey = Literal[
    "ai.budget",
    "ai.forecast",
    "ai.smart_plan",
    "ai.autocategorize",
    "ai.agent",
    "plans",
]

ALL_FEATURE_KEYS: frozenset[str] = frozenset(get_args(FeatureKey))

# TBD-559: a MODULE is what the operator sells: catalog metadata grouping
# feature keys and usage meters. Every key and every meter sits in exactly one
# module (fenced in tests/test_feature_catalog_frontend_contract.py, mirrored in
# frontend/lib/feature-catalog.ts through the generated fixture). Storage stays
# per key, so a plan can grant part of a module.
FEATURE_MODULES: dict[str, tuple[str, ...]] = {
    "ai": ("ai.agent", "ai.autocategorize", "ai.budget", "ai.forecast", "ai.smart_plan"),
    "plans": ("plans",),
}

MeterKey = Literal[
    "assistant.turns",
    "mcp.calls",
    "platform_ai.tokens",
    "platform_ai.cents",
]

ALL_METER_KEYS: frozenset[str] = frozenset(get_args(MeterKey))

Period = Literal["day", "month"]

# Meter name -> module. Counted in usage_counters and limited by
# feature_service.get_entitlements (TBD-585); the names are a stored contract.
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
    plans:             StrictBool = Field(default=False, alias="plans")


class MeterLimit(BaseModel):
    """One meter's limit: ``limit`` None is unlimited, 0 is none at all."""

    model_config = ConfigDict(extra="forbid")

    period: Period
    limit: Annotated[StrictInt, Field(ge=0, le=2**53 - 1)] | None


def _unlimited() -> MeterLimit:
    return MeterLimit(period="month", limit=None)


def _none_at_all() -> MeterLimit:
    return MeterLimit(period="month", limit=0)


class PlanUsageLimits(BaseModel):
    """Canonical persisted shape of plans.usage_limits (TBD-585).

    Missing meters take their default, so ``{}`` is a valid (default) plan.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    assistant_turns:    MeterLimit = Field(default_factory=_unlimited, alias="assistant.turns")
    mcp_calls:          MeterLimit = Field(default_factory=_unlimited, alias="mcp.calls")
    platform_ai_tokens: MeterLimit = Field(default_factory=_none_at_all, alias="platform_ai.tokens")
    platform_ai_cents:  MeterLimit = Field(default_factory=_none_at_all, alias="platform_ai.cents")

    @model_validator(mode="after")
    def _platform_meters_are_bounded(self) -> "PlanUsageLimits":
        for name, field in type(self).model_fields.items():
            if field.alias.startswith("platform_ai.") and getattr(self, name).limit is None:
                raise ValueError(f"{field.alias} spends platform money and cannot be unlimited")
        return self
