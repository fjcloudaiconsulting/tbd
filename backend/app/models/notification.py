"""Per-user notification substrate for sensitive operations.

Two tables live here (spec
``specs/2026-05-21-notification-system-sensitive-ops.md`` + 2nd-arch
delta ``specs/2026-05-22-notification-system-2nd-arch-pass.md``):

- ``notifications`` — per-user feed rows written by sensitive-op
  routes. Two mutable columns: ``seen_at`` (cleared on bell-open) and
  ``read_at`` (cleared on row-click). ``audit_event_id`` is the
  forensic correlation back to the matching audit row, nullable
  + ``ON DELETE SET NULL`` so audit row deletion does not orphan
  the notification feed.
- ``user_notification_preferences`` — one row per user, five
  categories x two channels (``cc_statement`` added by migration 076
  for CC Statement Alerts V1). ``email_security`` is forced TRUE at
  the API layer (the column shape is preserved so a future "really
  opt me out" exception is a one-line change). Auto-created on first
  access via the service layer.

Architect-locked decisions baked into this module:

- ``seen_at`` + ``audit_event_id`` columns are present from PR1's
  create migration (2nd-arch delta G1 + G5).
- Hardcoded English ``title`` / ``body`` for v1; the template module
  ``notification_templates.py`` centralizes them.
- BigInteger id on MySQL — notification feeds grow unbounded, same
  reasoning as ``audit_events``. SQLite test path uses INTEGER via
  ``with_variant`` so the in-memory autoincrement stays honest.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class NotificationCategory(str, enum.Enum):
    """Coarse grouping that maps 1:1 to a preference toggle."""

    SECURITY = "security"
    ACCOUNT = "account"
    ORG_ADMIN = "org_admin"
    ORG_ACTIVITY = "org_activity"
    CC_STATEMENT = "cc_statement"


class Notification(Base):
    __tablename__ = "notifications"

    # BigInteger on MySQL; SQLite test path uses INTEGER via
    # with_variant so autoincrement stays available.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    category: Mapped[NotificationCategory] = mapped_column(
        Enum(
            NotificationCategory,
            name="notification_category",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
    )
    # Mirrors the corresponding audit_events.event_type so an operator
    # can correlate via event_type + timestamp + user_id even when the
    # explicit audit_event_id FK is NULL.
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Relative app path (e.g. /settings/security) so the user can
    # jump to the affected screen. May be NULL when the action has
    # no destination (e.g. account.deleted).
    link_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # Cleared on bell-open (POST /mark-seen). Clears the badge.
    seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    # Cleared on row-click (PATCH /{id}) or read-all. The "unread"
    # visual state in the inbox list.
    read_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    # Forensic correlation back to the audit row that caused this
    # notification, when there is one. ON DELETE SET NULL so audit
    # row deletion (rare, but possible via admin tooling) does not
    # cascade-kill the notification feed. No index — we don't query
    # notifications BY audit_event_id; the column is for forensic
    # lookups only.
    audit_event_id: Mapped[Optional[int]] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("audit_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(6),
        nullable=False,
    )

    __table_args__ = (
        # Covers the unseen-count badge query
        #   WHERE user_id = ? AND seen_at IS NULL
        Index(
            "ix_notifications_user_unseen",
            "user_id",
            "seen_at",
            "created_at",
        ),
        # Covers the inbox feed and unread filter
        #   WHERE user_id = ? ORDER BY created_at DESC
        #   WHERE user_id = ? AND read_at IS NULL ORDER BY created_at DESC
        Index(
            "ix_notifications_user_unread",
            "user_id",
            "read_at",
            "created_at",
        ),
        Index("ix_notifications_event_type", "event_type"),
    )


class UserNotificationPreferences(Base):
    __tablename__ = "user_notification_preferences"

    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # Email channel toggles. email_security is forced TRUE at the API
    # layer — PUT /preferences raises 400 code=security_emails_required
    # when the request carries email_security=False. The column shape
    # is preserved so a future "really opt me out of security emails"
    # exception is a one-line change.
    email_security: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    email_account: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    email_org_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    # Org activity ("who did what in your org" feed) is noisy by
    # nature; default OFF and opt-in.
    email_org_activity: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    # In-app toggles mirror the email defaults. Same security force-on
    # rule applies at the API layer.
    in_app_security: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    in_app_account: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    in_app_org_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    in_app_org_activity: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    # CC statement alerts (reminder + close). Default ON/opt-out,
    # mirroring email_account/in_app_account (NOT the opt-in
    # org_activity shape) — these are money-timing signals, not noise.
    email_cc_statement: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    in_app_cc_statement: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
