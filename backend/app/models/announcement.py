"""Operator-authored announcement banners (spec 2026-05-21).

Two tables live here:

- ``announcements``: the operator-authored content rows. Plain-text
  body (auto-linkified at render time on the frontend, no markdown or
  HTML); severity drives styling and dismissibility.
- ``user_dismissed_announcements``: per-user dismissal join table. The
  composite PK ``(user_id, announcement_id)`` makes a double-dismiss
  POST idempotent at the DB level. ``ON DELETE CASCADE`` on both FKs
  so dropping either side cleans up the join row.

Architect-locked decisions baked into the schema (spec §"Architect
resolutions"):

- Integer PK (no UUID).
- ``title`` is required (NOT NULL).
- Three severities; ``maintenance`` is force-shown (no dismiss
  button rendered on the FE AND the dismiss endpoint rejects with
  ``code=announcement_not_dismissible``).
- ``end_at`` MUST be after ``start_at``. Pydantic enforces it at the
  schema layer; this module's only column-level guarantee is the
  index that lets the active-window filter stay cheap.
- ``created_by_user_id`` is ``ON DELETE SET NULL`` so deleting the
  authoring superadmin leaves the announcement and its dismissal
  history intact (mirrors the ``audit_events`` pattern).
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
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


class AnnouncementSeverity(str, enum.Enum):
    INFO = "info"
    PROMO = "promo"
    MAINTENANCE = "maintenance"


class Announcement(Base):
    __tablename__ = "announcements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[AnnouncementSeverity] = mapped_column(
        Enum(
            AnnouncementSeverity,
            name="announcement_severity",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=AnnouncementSeverity.INFO,
        server_default=AnnouncementSeverity.INFO.value,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    # Schedule window. Both NULLABLE; NULL on either side means
    # "unbounded" in that direction. Pydantic enforces end_at > start_at
    # when both are present.
    start_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    end_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_by_user_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        # Covers the active-window read path:
        #   WHERE is_active = TRUE
        #     AND (start_at IS NULL OR start_at <= NOW())
        #     AND (end_at IS NULL OR end_at > NOW())
        Index(
            "ix_announcements_active_window",
            "is_active",
            "start_at",
            "end_at",
        ),
    )


class UserDismissedAnnouncement(Base):
    __tablename__ = "user_dismissed_announcements"

    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    announcement_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("announcements.id", ondelete="CASCADE"),
        primary_key=True,
    )
    dismissed_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        nullable=False,
    )
