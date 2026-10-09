from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, StrictBool


class AIFeatureState(BaseModel):
    entitled: StrictBool
    configured: StrictBool


class MeterUsage(BaseModel):
    """This period's use of one usage meter; ``limit`` None is unlimited, 0 is
    closed (and never resets)."""

    used: int
    limit: Optional[int]
    period: Literal["day", "month"]
    resets_at: Optional[datetime]


class AIStatusResponse(BaseModel):
    categorize: AIFeatureState
    forecast: AIFeatureState
    budget: AIFeatureState
    agent: AIFeatureState
    usage: dict[str, MeterUsage] = {}
