"""Pydantic schemas for the in-app feedback widget.

Privacy posture lives in the FIELD DEFAULTS, not in side-channel
config: `include_identity` defaults to False so the typed contract
matches the spec's "default unchecked" rule. The frontend never has
to remember to set this; omission yields anonymity.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.feedback import FeedbackCategory


# Message bounds. 5000 chars is generous for the longest "here's my
# whole bug repro" submission but bounded so we never accept a 50MB
# paste. Enforced at the schema layer so FastAPI returns 422 before
# the request reaches the service.
FEEDBACK_MESSAGE_MIN_LENGTH = 1
FEEDBACK_MESSAGE_MAX_LENGTH = 5000


class FeedbackContext(BaseModel):
    """Client-collected operational context.

    Every field is optional on the wire — the frontend collects what
    it can but the server tolerates partial captures (e.g. no
    `app_version` env var set). The service layer normalizes and
    re-validates before persisting, so trust-but-verify is the rule.

    PII INVARIANT: no field here is allowed to carry account names,
    balances, transaction descriptions, or identifying URL fragments.
    The router strips query strings off `url` before storing.
    """
    model_config = ConfigDict(extra="ignore")

    url: Optional[str] = Field(default=None, max_length=512)
    user_agent: Optional[str] = Field(default=None, max_length=512)
    app_version: Optional[str] = Field(default=None, max_length=64)
    viewport_w: Optional[int] = Field(default=None, ge=0, le=20000)
    viewport_h: Optional[int] = Field(default=None, ge=0, le=20000)
    theme: Optional[str] = Field(default=None, max_length=32)


class FeedbackCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(
        min_length=FEEDBACK_MESSAGE_MIN_LENGTH,
        max_length=FEEDBACK_MESSAGE_MAX_LENGTH,
    )
    category: FeedbackCategory
    # Default OFF — the privacy contract. Frontend explicitly sets True
    # only when the user ticks the opt-in box.
    include_identity: bool = False
    context: FeedbackContext = Field(default_factory=FeedbackContext)


class FeedbackResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    category: FeedbackCategory
    created_at: datetime
    # No message echo by default — keeps the success path lean and
    # prevents accidental leak via response logs. Admin reads use
    # a separate (future) admin schema.
