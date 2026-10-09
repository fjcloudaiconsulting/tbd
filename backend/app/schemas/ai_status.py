from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, StrictBool


class AIFeatureState(BaseModel):
    entitled: StrictBool
    configured: StrictBool


class MeterUsage(BaseModel):
    """This period's use of one usage meter; ``limit`` None is unlimited."""

    meter: str
    used: int
    limit: Optional[int]
    period: Literal["day", "month"]
    resets_at: datetime


class AIStatusResponse(BaseModel):
    categorize: AIFeatureState
    forecast: AIFeatureState
    budget: AIFeatureState
    agent: AIFeatureState
    meters: list[MeterUsage] = []
