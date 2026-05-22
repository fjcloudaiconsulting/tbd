"""Pydantic schemas for the rate-limit override system (L4.10).

Architect-locked invariants enforced here:

- A row carries exactly one scope: ``org_id`` XOR ``user_id``. Sending
  both, or neither, returns 422 before the row ever reaches the DB.
- ``max_requests`` is bounded ``[1, 100000]``. Lower bound 1 (not 0)
  prevents the documented self-lockout footgun where a superadmin
  could pin themselves to ``0`` and lose access to every gated route.
  Upper bound 100000 is generous and bounded so a typo cannot wedge
  the limiter cache with a 64-bit integer.
- ``period_seconds`` is bounded ``[1, 86400]``. One day is the longest
  practical bucket; periods beyond that should be a policy change,
  not an override.
- ``endpoint_pattern`` is a short opaque string (max 80 chars,
  matching the column width) AND must appear in
  ``app.rate_limit_endpoint_catalogue.RATE_LIMITED_ENDPOINT_PATTERNS``.
  Free-form strings that no decorator references silently no-op at
  request time, so the catalogue check at the schema layer surfaces
  the typo as a 422 with the full catalogue echoed back in the
  error body.
- ``expires_at``, when sent, must be strictly in the future. A row
  with ``expires_at`` in the past is treated as inert by the resolver
  but creating one in the past is rejected here so the admin UI
  surfaces it as a user error, not a silent no-op.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.rate_limit_endpoint_catalogue import (
    RATE_LIMITED_ENDPOINT_PATTERNS,
    sorted_patterns,
)


# Bounds matching the migration column widths. Pydantic surfaces a
# precise 422 instead of letting the DB-side StringDataRightTruncation
# bubble back as a generic 500.
ENDPOINT_PATTERN_MIN = 1
ENDPOINT_PATTERN_MAX = 80


def _validate_endpoint_pattern(value: str) -> str:
    """Reject patterns the codebase has no decorator for.

    The error message lists the full catalogue so the API caller (and
    the admin UI) can recover without a separate round-trip. The list
    is sorted for determinism in tests + UI parity.
    """
    if value not in RATE_LIMITED_ENDPOINT_PATTERNS:
        catalogue = ", ".join(sorted_patterns())
        raise ValueError(
            f"unknown endpoint_pattern {value!r}. Valid patterns: {catalogue}"
        )
    return value


NOTE_MAX = 5000

# Self-lockout guard: max_requests must be >= 1. See module docstring.
MAX_REQUESTS_MIN = 1
MAX_REQUESTS_MAX = 100_000

PERIOD_SECONDS_MIN = 1
PERIOD_SECONDS_MAX = 86_400  # 24h


def _utcnow() -> datetime:
    """Naive UTC ``now()`` matching the app's storage convention.

    The DB writes ``DateTime`` columns naive; comparing ``expires_at``
    (naive) against an aware UTC ``now()`` would raise. We normalise
    here so the validator works regardless of how the caller supplies
    the timestamp.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class _ScopeXorMixin(BaseModel):
    """Enforces exactly-one-of ``org_id`` / ``user_id``.

    Subclasses MUST declare both ``org_id`` and ``user_id``. The
    create payload is strict (one and only one); the update payload
    forbids changing scope at all (separate validator) so this mixin
    is only used on Create.
    """

    @model_validator(mode="after")
    def _check_scope_xor(self):
        org_id = getattr(self, "org_id", None)
        user_id = getattr(self, "user_id", None)
        if (org_id is None) == (user_id is None):
            raise ValueError(
                "exactly one of org_id or user_id must be set",
            )
        return self


class RateLimitOverrideCreate(_ScopeXorMixin):
    model_config = ConfigDict(extra="forbid")

    org_id: Optional[int] = Field(default=None, ge=1)
    user_id: Optional[int] = Field(default=None, ge=1)
    endpoint_pattern: str = Field(
        min_length=ENDPOINT_PATTERN_MIN,
        max_length=ENDPOINT_PATTERN_MAX,
    )
    max_requests: int = Field(ge=MAX_REQUESTS_MIN, le=MAX_REQUESTS_MAX)
    period_seconds: int = Field(ge=PERIOD_SECONDS_MIN, le=PERIOD_SECONDS_MAX)
    expires_at: Optional[datetime] = None
    note: Optional[str] = Field(default=None, max_length=NOTE_MAX)

    @field_validator("endpoint_pattern")
    @classmethod
    def _endpoint_in_catalogue(cls, value: str) -> str:
        return _validate_endpoint_pattern(value)

    @model_validator(mode="after")
    def _expires_in_future(self):
        if self.expires_at is not None:
            # Coerce aware -> naive UTC for direct comparison with
            # ``_utcnow()``. Both shapes are accepted on the wire.
            cmp = self.expires_at
            if cmp.tzinfo is not None:
                cmp = cmp.astimezone(timezone.utc).replace(tzinfo=None)
            if cmp <= _utcnow():
                raise ValueError("expires_at must be in the future")
        return self


class RateLimitOverrideUpdate(BaseModel):
    """Partial update. Scope (``org_id`` / ``user_id``) is immutable
    once written, so neither key appears here. ``endpoint_pattern`` is
    mutable so an operator can correct a typo without re-creating the
    row (which would lose the audit trail tied to the row id).
    """

    model_config = ConfigDict(extra="forbid")

    endpoint_pattern: Optional[str] = Field(
        default=None,
        min_length=ENDPOINT_PATTERN_MIN,
        max_length=ENDPOINT_PATTERN_MAX,
    )
    max_requests: Optional[int] = Field(
        default=None, ge=MAX_REQUESTS_MIN, le=MAX_REQUESTS_MAX
    )
    period_seconds: Optional[int] = Field(
        default=None, ge=PERIOD_SECONDS_MIN, le=PERIOD_SECONDS_MAX
    )
    expires_at: Optional[datetime] = None
    # ``note`` is the only field where "explicit null" is a meaningful
    # request: it clears the note. We accept that by leaving it
    # ``Optional[str]`` without a non-null guard.
    note: Optional[str] = Field(default=None, max_length=NOTE_MAX)

    @field_validator("endpoint_pattern")
    @classmethod
    def _endpoint_in_catalogue(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return _validate_endpoint_pattern(value)


class RateLimitOverrideResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: Optional[int] = None
    user_id: Optional[int] = None
    endpoint_pattern: str
    max_requests: int
    period_seconds: int
    expires_at: Optional[datetime] = None
    created_by_user_id: Optional[int] = None
    note: Optional[str] = None
    created_at: datetime
    updated_at: datetime
