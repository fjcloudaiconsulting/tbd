"""Pydantic schemas for the superadmin email broadcast system (spec
``2026-07-18-admin-email-broadcast-design.md``).

- ``BroadcastCreate`` is the authoring payload: subject + body template
  (may contain the literal ``{first_name}`` token, substituted per
  recipient at render time — see ``app.services.broadcast_service``)
  plus the target ``segment``. ``segment`` is a plain bounded string,
  not an enum (Ruling 4 — it is the axis designed to grow and native
  MySQL ENUM there would hit the ALTER-ENUM landmine).
- ``BroadcastResponse`` is the read shape: every model column plus a
  computed ``recipient_count`` the router fills in (materialized count
  once sending has started, else a live segment count for a draft).
- ``BroadcastSendRequest`` is the typed-confirm payload gating
  ``POST /{id}/send`` — the operator must echo back the subject and the
  recipient count they were shown so a stale tab can't fire a send.
- ``PreviewResponse`` is the read-only rendered preview returned by
  ``GET /{id}/preview`` (not persisted).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.email_broadcast import BroadcastStatus

SUBJECT_MIN_LENGTH = 1
SUBJECT_MAX_LENGTH = 200
BODY_TEMPLATE_MIN_LENGTH = 1


class BroadcastCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=SUBJECT_MIN_LENGTH, max_length=SUBJECT_MAX_LENGTH)
    body_template: str = Field(min_length=BODY_TEMPLATE_MIN_LENGTH)
    segment: str


class BroadcastResponse(BaseModel):
    """Read shape for a broadcast row plus a computed recipient count.

    ``recipient_count`` is not a DB column — the router fills it in
    (live segment count while DRAFT, else the materialized
    ``total_recipients``) since a draft's audience can shift right up
    until send-time.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    subject: str
    body_template: str
    segment: str
    status: BroadcastStatus
    created_by_user_id: Optional[int] = None
    total_recipients: Optional[int] = None
    sent_count: int
    failed_count: int
    skipped_count: int
    dry_run_sent_at: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    recipient_count: Optional[int] = None


class BroadcastSendRequest(BaseModel):
    """Typed-confirm payload for ``POST /{id}/send``.

    The operator must echo back the exact subject and recipient count
    they were shown on the preview/dry-run screen. A mismatch on either
    field is a 422 (stale tab / changed audience since the confirm
    dialog was rendered), never a silent send.
    """

    model_config = ConfigDict(extra="forbid")

    confirm_subject: str
    confirm_recipient_count: int


class PreviewResponse(BaseModel):
    """Rendered (not persisted) preview returned by ``GET /{id}/preview``."""

    subject: str
    html: str
    text: str
