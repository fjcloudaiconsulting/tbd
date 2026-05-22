"""Pydantic schemas for AI spend caps (PR1 follow-up).

PR1 only ships CRUD; PR2 wires the cap-check / ledger into ``call_llm``.
Both soft and hard caps are nullable INT (cents); either can be unset
to mean "no cap on that axis". A row with both nulls is allowed (it's
a vestigial empty record); the call site reads it as "no cap".
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


FEATURE_KEY_MAX_LENGTH = 120


class CapsWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    soft_cap_cents: Optional[int] = Field(default=None, ge=0)
    hard_cap_cents: Optional[int] = Field(default=None, ge=0)
    period: Literal["monthly"] = "monthly"

    @model_validator(mode="after")
    def _hard_ge_soft(self):
        if (
            self.soft_cap_cents is not None
            and self.hard_cap_cents is not None
            and self.hard_cap_cents < self.soft_cap_cents
        ):
            raise ValueError(
                "hard_cap_cents must be >= soft_cap_cents when both are set"
            )
        return self


class DefaultCapsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    org_id: int
    soft_cap_cents: Optional[int]
    hard_cap_cents: Optional[int]
    period: str
    created_at: datetime
    updated_at: datetime


class FeatureCapsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    org_id: int
    feature_key: str
    soft_cap_cents: Optional[int]
    hard_cap_cents: Optional[int]
    period: str
    created_at: datetime
    updated_at: datetime


class CapsBundleResponse(BaseModel):
    default: Optional[DefaultCapsResponse] = None
    features: list[FeatureCapsResponse] = Field(default_factory=list)
