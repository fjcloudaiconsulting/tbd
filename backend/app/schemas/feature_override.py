"""Request/response schemas for org feature overrides."""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class FeatureOverrideUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value:      StrictBool
    expires_at: datetime | None = None
    note:       str | None = Field(default=None, max_length=500)


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
