"""Pydantic schemas for AI provider routing (PR1 follow-up).

Routing tables are split (default vs feature, see model docstring).
The router presents one combined GET that returns both, plus separate
PUT/DELETE endpoints per shape.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


MODEL_NAME_MAX_LENGTH = 120
FEATURE_NAME_MAX_LENGTH = 120


class DefaultRoutingWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential_id: int = Field(gt=0)
    model: str = Field(min_length=1, max_length=MODEL_NAME_MAX_LENGTH)


class FeatureRoutingWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential_id: int = Field(gt=0)
    model: str = Field(min_length=1, max_length=MODEL_NAME_MAX_LENGTH)


class DefaultRoutingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    org_id: int
    credential_id: int
    model: str
    created_at: datetime
    updated_at: datetime


class FeatureRoutingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    org_id: int
    feature_name: str
    credential_id: int
    model: str
    created_at: datetime
    updated_at: datetime


class RoutingBundleResponse(BaseModel):
    default: Optional[DefaultRoutingResponse] = None
    features: list[FeatureRoutingResponse] = Field(default_factory=list)
