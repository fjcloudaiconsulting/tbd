"""Pydantic schemas for L4.3 admin org management."""
from __future__ import annotations

import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class SubscriptionUpdateRequest(BaseModel):
    plan_id: Optional[int] = None
    status: Optional[Literal["trialing", "active", "past_due", "canceled"]] = None
    trial_end: Optional[datetime.date] = None
    current_period_end: Optional[datetime.date] = None


class OrgDeleteRequest(BaseModel):
    confirm_name: str = Field(min_length=1, max_length=200)
