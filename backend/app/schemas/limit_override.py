"""Request/response schemas for org usage-limit overrides (TBD-585)."""
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from app.auth.feature_catalog import Period
from app.schemas.feature_override import expires_at_to_naive_utc


class LimitOverrideUpsert(BaseModel):
    """``period`` and ``limit_value`` are both REQUIRED: an omitted limit must
    never read as "unlimited". ``limit_value`` null is an explicit unlimited."""

    model_config = ConfigDict(extra="forbid")

    period:      Period
    limit_value: Annotated[StrictInt, Field(ge=0, le=2**53 - 1)] | None
    expires_at:  datetime | None = None
    note:        str | None = Field(default=None, max_length=500)

    _expires_naive = field_validator("expires_at")(expires_at_to_naive_utc)


class OrgLimitOverrideResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    meter: str
    period: str
    limit_value: int | None
    set_by: int | None
    set_by_email: str | None
    set_at: datetime
    expires_at: datetime | None
    note: str | None
    is_expired: bool
