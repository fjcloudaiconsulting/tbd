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

from app.models.email_broadcast import BroadcastStatus, RecipientStatus

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
    # Batch-sending model (architect Ruling R1, 2026-07-19): a broadcast
    # sends via Mailgun batch calls, which report only per-batch acceptance.
    # ``sent_count`` therefore means "accepted by the mail provider for
    # delivery" — NOT confirmed delivered (true delivered/bounced status
    # needs the deferred Mailgun webhooks). The UI MUST label this "Queued"
    # or "Accepted for delivery", never "Delivered" or bare "Sent".
    sent_count: int = Field(
        description="Recipients accepted by the mail provider for delivery "
        "(NOT confirmed delivered). Label as 'Queued' / 'Accepted', not 'Delivered'.",
    )
    failed_count: int = Field(
        description="Recipients in batches the mail provider rejected (non-2xx).",
    )
    skipped_count: int = Field(
        description="Targeted users who lapsed (inactive/unverified) before send.",
    )
    dry_run_sent_at: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    recipient_count: Optional[int] = None
    # Derived, compute-on-read Mailgun delivery counts (Ruling W9) — NEVER
    # stored columns, NEVER bumped by the webhook. Filled in by the router
    # from ``broadcast_service.delivery_counts`` /
    # ``delivery_counts_for_broadcasts``. Default 0 so a still-draft
    # broadcast (no recipient rows yet) reports zeros rather than nulls.
    delivered_count: int = 0
    bounced_count: int = 0
    soft_bounced_count: int = 0
    complained_count: int = 0


class RecipientResponse(BaseModel):
    """Read shape for one ``EmailBroadcastRecipient`` row, returned by
    ``GET /{broadcast_id}/recipients`` (Ruling W9). Superadmin-gated and
    operationally necessary (Ruling W10) so an operator can see WHICH
    addresses bounced/complained and clean up dead accounts."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    first_name: Optional[str] = None
    status: RecipientStatus
    delivery_status: Optional[str] = None
    delivery_updated_at: Optional[datetime] = None
    sent_at: Optional[datetime] = None


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
