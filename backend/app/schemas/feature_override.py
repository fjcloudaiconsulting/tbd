"""Request/response schemas for org feature overrides."""
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator


def expires_at_to_naive_utc(v: datetime | None) -> datetime | None:
    """Override expiry is stored naive UTC and compared on the app clock; an
    aware value would be stored in wall-clock terms by MySQL DATETIME."""
    if v is not None and v.tzinfo is not None:
        return v.astimezone(timezone.utc).replace(tzinfo=None)
    return v


class FeatureOverrideUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value:      StrictBool
    expires_at: datetime | None = None
    note:       str | None = Field(default=None, max_length=500)

    _expires_naive = field_validator("expires_at")(expires_at_to_naive_utc)


class OrgFeatureOverrideResponse(BaseModel):
    """The wire shape for an org feature override row.

    set_by_email is server-resolved from the joined users row.
    is_expired is server-derived: expires_at IS NOT NULL AND expires_at <= NOW().
    """
    model_config = ConfigDict(from_attributes=True)

    feature_key: str
    value: bool
    set_by: int | None
    set_by_email: str | None
    set_at: datetime
    expires_at: datetime | None
    note: str | None
    is_expired: bool
