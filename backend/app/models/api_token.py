"""Superadmin personal access tokens (PAT) — spec 2026-07-2x.

One row per issued token. Only the SHA-256 (HMAC-peppered — see
``app.security_pat`` from Task 1) ``token_hash`` is stored; the raw token is
shown to the superadmin exactly once at creation time and is never
persisted. ``token_prefix`` is a short, non-secret slice of the raw token
(e.g. ``pat_abcdefghij``) kept around purely so the admin UI can show "which
token is this" in a list without re-deriving anything secret.

``scope`` is an app-validated ``String(16)`` (``"read"`` | ``"write"``,
write superset of read), NOT a native DB enum — same discipline as
``email_broadcast.segment`` (see that module's docstring): this is the axis
most likely to grow fine-grained scopes later, and a native MySQL ENUM here
would hit the ALTER-ENUM landmine (green on SQLite CI, 500 on prod).

``created_by_user_id`` is ``ON DELETE SET NULL`` and ``created_by_email`` is
a snapshot taken at issuance time, matching the ``audit_events`` /
``email_broadcast_recipients`` convention: the record of who minted a token
must survive that user's later deletion.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ApiToken(Base):
    __tablename__ = "api_tokens"

    # BigInteger on MySQL, Integer on SQLite (autoincrement only honours
    # INTEGER there) — same `with_variant` trick as `audit_events.id`.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # App-validated "read" | "write" (write superset of read). String, NOT
    # Enum — see module docstring.
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_user_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Snapshot — the issuing admin's email at creation time, never resolved
    # through the FK (which can be NULL after user deletion).
    created_by_email: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # func.now() (not sa.text("now()")) so this compiles per-dialect —
    # NOW() on MySQL, CURRENT_TIMESTAMP on SQLite — matching
    # email_broadcast.py / audit_event.py; a raw "now()" text default
    # fails SQLite's in-memory model tests (no now() function there),
    # even though the migration's own sa.text("now()") is fine since
    # migrations only ever run against real MySQL.
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_used_ip: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    reminder_stage: Mapped[int] = mapped_column(
        SmallInteger, default=0, server_default=text("0"), nullable=False
    )
