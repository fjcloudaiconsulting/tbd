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


# Columns on ``announcements`` that a PATCH update rejects explicit
# ``null`` for. Architect-locked PR #340 review (2026-05-22):
# ``title``, ``severity``, ``start_at``, ``body`` are the four called
# out by name; ``is_active`` is the bool column (NULL would surface as
# a generic 500 the same way the four named columns would). Sending
# any of these as ``null`` returns a deterministic 422 with a
# field-level error BEFORE any DB write. ``end_at`` is the one
# legitimately nullable column — clearing a scheduled end via
# ``PATCH {"end_at": null}`` stays 200.
_NON_NULLABLE_UPDATE_KEYS = frozenset(
    {"title", "body", "severity", "is_active", "start_at"}
)


class AnnouncementUpdate(_ScheduleValidatedMixin):
    """Partial update payload. Every column is *partial* (key may be
    missing), but non-nullable columns reject an explicit ``null``.

    Architect-locked contract (PR #340 review, 2026-05-22):

    - ``title``, ``body``, ``severity``, ``start_at`` are non-nullable
      on the DB row. An update payload that *omits* the key is fine
      (means "leave this field alone"), but a payload that sends the
      key with an explicit ``null`` must return a deterministic 422
      with a field-level error BEFORE any DB write. Pydantic v2's
      ``model_validator(mode="before")`` enforces this without
      coupling the type to ``Optional[...]`` (which would silently
      accept ``null`` and then explode at SQLAlchemy / enum-coerce
      time as a generic 500).
    - ``end_at`` is the one legitimately nullable column — sending
      ``end_at: null`` means "clear the scheduled end", and stays
      200.
    - ``is_active`` is a bool; sending ``null`` is rejected with the
      same field-level 422 as the strings.

    Cross-call schedule invariant (``end_at > start_at`` against the
    *merged* existing + patch state) is still enforced by the router
    because the per-payload mixin only sees what the request carries.
    """

    model_config = ConfigDict(extra="forbid")

    # The fields stay Optional so a missing key is fine, but the
    # ``mode="before"`` validator below intercepts and rejects any
    # *explicit* ``null`` on the non-nullable columns. This keeps the
    # "field key missing" vs "field key present and null" distinction
    # that the bare ``Optional[X] = None`` shape destroys.
    title: Optional[str] = Field(
        default=None, min_length=TITLE_MIN_LENGTH, max_length=TITLE_MAX_LENGTH
    )
    body: Optional[str] = Field(
        default=None, min_length=BODY_MIN_LENGTH, max_length=BODY_MAX_LENGTH
    )
    severity: Optional[AnnouncementSeverity] = None
    is_active: Optional[bool] = None
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None  # genuinely nullable on the DB

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_null_for_non_nullable(cls, data):
        # Pydantic feeds dicts directly when called from JSON. If the
        # caller already passed a model instance we don't have to
        # check — there's no way to construct the model with explicit
        # nulls on non-nullable fields via the typed API. Only the
        # raw-payload path needs the gate.
        if not isinstance(data, dict):
            return data
        offending = [
            key
            for key in _NON_NULLABLE_UPDATE_KEYS
            if key in data and data[key] is None
        ]
        if offending:
            # Pydantic v2 surfaces this as a 422 with per-field
            # location info matching the standard validation envelope
            # (loc=("title",), msg=..., type="value_error"). The
            # router does not have to short-circuit anything.
            raise ValueError(
                "field cannot be null: " + ", ".join(sorted(offending))
            )
        return data


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
