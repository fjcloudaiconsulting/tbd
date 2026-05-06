"""Durable audit log for superadmin platform actions (L4.7).

The structlog ``admin.org.*`` and ``org.data.*`` events emitted by the
admin and tenant routers stream to stdout (and from there to whatever
log sink ops wires up). They're great for triage but they're not a
queryable history with SLA-grade retention. This table persists the
same events into a durable, indexable store so superadmins can answer
"who did what to whom, and when" from the admin UI without grepping
container logs.

Two design choices worth restating in code:

1. **Independent-session writes.** The recording function opens its
   own ``AsyncSession`` from the engine-wide factory, commits, and
   swallows exceptions after logging. An audit-write failure must
   never poison the business transaction it describes — and a
   business rollback (e.g. ``admin.org.delete.failed``) must still
   produce an audit row, which only works if the audit write isn't
   inside the rolled-back txn.

2. **Survives org wipe.** ``audit_events.target_org_id`` uses
   ``ON DELETE SET NULL`` so deleting an organization (or wiping
   its data via the tenant reset path) leaves the audit history
   intact. The ``target_org_name`` snapshot column preserves the
   org name at the moment of the event, which is the only sane
   thing to display in the UI after the org is gone. Same trick for
   ``actor_user_id`` / ``actor_email``.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AuditOutcome(str, enum.Enum):
    SUCCESS = "success"
    FAILURE = "failure"


class AuditEvent(Base):
    __tablename__ = "audit_events"

    # BigInteger on MySQL (audit logs grow forever and we don't want
    # to wedge against the 32-bit ceiling), but SQLite's autoincrement
    # only honours INTEGER (not BIGINT) — `with_variant` keeps the
    # in-memory test path on a real autoincrementing column.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    event_type: Mapped[str] = mapped_column(
        String(80), nullable=False, index=True
    )
    actor_user_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Snapshot — the actor's email at event time, never resolved
    # through the FK (which can be NULL after user deletion).
    actor_email: Mapped[str] = mapped_column(String(255), nullable=False)
    target_org_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Snapshot — same rationale as actor_email.
    target_org_name: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True
    )
    request_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    outcome: Mapped[AuditOutcome] = mapped_column(
        Enum(AuditOutcome, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    detail: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        server_default=func.now(6),
        nullable=False,
        index=True,
    )
