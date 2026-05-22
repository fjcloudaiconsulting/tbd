"""Pydantic schemas for the announcement banner system.

Architect-locked rules baked into this module (spec
``2026-05-21-announcement-banner-system.md``):

- ``end_at`` must be strictly after ``start_at`` whenever both are
  set. Enforced via ``model_validator`` on every shape that accepts
  either column (create + update), so the FE never has to guess
  which payload is valid.
- Body is plain text. We never accept HTML or markdown on the wire
  and the FE renders the value through an auto-linkifier — no schema
  rendering / sanitization happens here. Length is bounded so a
  pathological 50MB paste returns 422 before it touches the DB.
- ``title`` is required and bounded.
- Update payload allows partial edits (every field optional) but
  carries the same end_at > start_at invariant when both are present.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.announcement import AnnouncementSeverity


# Bounds matching the migration column widths. Pydantic surfaces a
# precise 422 instead of letting the DB-side StringDataRightTruncation
# bubble back as a generic 500.
TITLE_MIN_LENGTH = 1
TITLE_MAX_LENGTH = 200
# 5000 chars covers the longest "we are doing maintenance on..." blurb
# without giving operators a place to paste a novel. Aligned with the
# feedback widget's body cap for consistency.
BODY_MIN_LENGTH = 1
BODY_MAX_LENGTH = 5000


class _ScheduleValidatedMixin(BaseModel):
    """Mixin enforcing ``end_at > start_at`` when both are present.

    Subclasses MUST declare ``start_at`` and ``end_at`` fields. The
    validator is permissive on either side being NULL (one-sided
    open windows are allowed) and only fires when both are populated.
    """

    @model_validator(mode="after")
    def _check_window(self):
        start_at = getattr(self, "start_at", None)
        end_at = getattr(self, "end_at", None)
        if start_at is not None and end_at is not None and end_at <= start_at:
            raise ValueError("end_at must be strictly after start_at")
        return self


class AnnouncementCreate(_ScheduleValidatedMixin):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=TITLE_MIN_LENGTH, max_length=TITLE_MAX_LENGTH)
    body: str = Field(min_length=BODY_MIN_LENGTH, max_length=BODY_MAX_LENGTH)
    severity: AnnouncementSeverity = AnnouncementSeverity.INFO
    is_active: bool = True
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None


class AnnouncementUpdate(_ScheduleValidatedMixin):
    """Partial update payload. Every column is optional, but if both
    ends of the schedule window are present (either freshly supplied
    or implied by the existing row plus the patch) the strict
    ``end_at > start_at`` invariant still has to hold.

    NOTE: the validator below only sees what the request carries, so
    the router service-layer enforcement re-checks against the merged
    (existing + patch) state to catch the cross-call case.
    """

    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = Field(
        default=None, min_length=TITLE_MIN_LENGTH, max_length=TITLE_MAX_LENGTH
    )
    body: Optional[str] = Field(
        default=None, min_length=BODY_MIN_LENGTH, max_length=BODY_MAX_LENGTH
    )
    severity: Optional[AnnouncementSeverity] = None
    is_active: Optional[bool] = None
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None


class AnnouncementResponse(BaseModel):
    """User-facing read shape — what /api/v1/announcements returns.

    Identical to the admin read shape today; kept as its own class so
    a future audit-shape divergence (e.g. exposing
    ``created_by_user_id`` only to superadmins) doesn't require a
    breaking refactor on the customer side.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    body: str
    severity: AnnouncementSeverity
    is_active: bool
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class AnnouncementAdminResponse(AnnouncementResponse):
    """Admin read shape — adds ``created_by_user_id`` for the admin UI
    so an operator can see who wrote the row.
    """

    created_by_user_id: Optional[int] = None
