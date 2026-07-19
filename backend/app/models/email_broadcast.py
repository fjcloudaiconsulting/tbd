"""Superadmin email broadcast (spec 2026-07-18).

Two tables live here:

- ``email_broadcasts``: one row per authored broadcast. ``body_template`` is
  stored raw (may contain the literal ``{first_name}`` token) and rendered
  per-recipient at send time, since the greeting differs per row. ``segment``
  is an app-validated ``String(32)``, NOT a DB enum (Ruling 4) — it is the
  axis designed to grow, and a native MySQL ENUM there would hit the
  ALTER-ENUM landmine (green on SQLite CI, 500 on prod). ``status`` is a
  closed set and stays a native ``Enum`` for DB integrity, matching
  ``announcement.py`` conventions.
- ``email_broadcast_recipients``: one row per targeted user, materialized at
  send time. ``email``/``first_name`` are SNAPSHOTS taken at materialization
  so the record of who was targeted survives a later email change or user
  deletion (``user_id`` is ``ON DELETE SET NULL``). The no-double-send
  guarantee is NOT the unique constraint alone — it is the per-row atomic
  claim in the drain (``UPDATE ... WHERE id=:rid AND status='pending'``,
  proceed only if ``rowcount == 1``); the constraint only stops duplicate
  INSERTs during materialization.

Architect-locked decisions (spec §"Architect rulings"):

- Ruling 4: ``segment`` → ``String(32)`` app-validated; ``status`` columns →
  native ``Enum(values_callable=..., name=...)``.
- Ruling 6: keep ``UNIQUE(broadcast_id, user_id)``, ``broadcast_id`` CASCADE,
  ``user_id`` SET NULL; add index ``(broadcast_id, status)`` to serve the
  drain's ``WHERE broadcast_id=? AND status='pending'`` select.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# v1 validation accepts exactly this one segment value (Ruling 10). Any new
# segment value, promotional/re-engagement content, or recurring send
# requires an unsubscribe + suppression mechanism first — see spec §"Audience"
# and §"Architect rulings" #10.
SEGMENT_ACTIVE_VERIFIED = "active_verified"


class BroadcastStatus(str, enum.Enum):
    DRAFT = "draft"
    SENDING = "sending"
    COMPLETED = "completed"
    FAILED = "failed"


class RecipientStatus(str, enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class EmailBroadcast(Base):
    __tablename__ = "email_broadcasts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    body_template: Mapped[str] = mapped_column(Text, nullable=False)
    # App-validated growth axis — NOT a DB enum. See module docstring + Ruling 4.
    segment: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[BroadcastStatus] = mapped_column(
        Enum(
            BroadcastStatus,
            name="broadcast_status",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=BroadcastStatus.DRAFT,
        server_default=BroadcastStatus.DRAFT.value,
    )
    created_by_user_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Populated at send-materialization time (not known while still draft).
    total_recipients: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # Gate for send: POST /{id}/send requires this to be set (mandatory
    # dry-run-to-self before any real send).
    dry_run_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        nullable=False,
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class EmailBroadcastRecipient(Base):
    __tablename__ = "email_broadcast_recipients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    broadcast_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("email_broadcasts.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Snapshots at materialization time — see module docstring.
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    first_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    status: Mapped[RecipientStatus] = mapped_column(
        Enum(
            RecipientStatus,
            name="broadcast_recipient_status",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=RecipientStatus.PENDING,
        server_default=RecipientStatus.PENDING.value,
    )
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        # Dedupes materialization INSERTs (one row per user per broadcast).
        UniqueConstraint("broadcast_id", "user_id", name="uq_broadcast_recipient"),
        # Serves the drain's WHERE broadcast_id=? AND status='pending' select.
        Index("ix_broadcast_recipient_status", "broadcast_id", "status"),
    )
