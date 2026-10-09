"""Session, single-use token and lease state: sessions move to MySQL (INFRA-122).

Written only by ``app.state_db``. Ids are case-sensitive (base64url jti), so
MySQL stores them ascii_bin; every time column is DATETIME(6) and set from the
database clock.
"""
from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def ascii_id(n: int = 64) -> sa.types.TypeEngine:
    return sa.String(n).with_variant(mysql.VARCHAR(n, charset="ascii", collation="ascii_bin"), "mysql")


def ts6() -> sa.types.TypeEngine:
    return sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


class AuthSessionFamily(Base):
    """One row per refresh-session family (sid): the lock point, the head jti
    and, by its absence, the revoke."""

    __tablename__ = "auth_session_families"

    sid: Mapped[str] = mapped_column(ascii_id(), primary_key=True)
    user_id: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    head_jti: Mapped[str] = mapped_column(ascii_id(), nullable=False)
    rotations: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    expires_at: Mapped[datetime] = mapped_column(ts6(), nullable=False)

    __table_args__ = (
        sa.Index("ix_auth_session_families_expires_at", "expires_at"),
        sa.Index("ix_auth_session_families_user_id", "user_id"),
    )


class AuthSessionMember(Base):
    """Every jti ever issued in a family (the old Redis family set). Insert-only:
    member ``seq`` was rotated out when member ``seq + 1`` was created, which
    is what bounds its grace window."""

    __tablename__ = "auth_session_members"

    jti: Mapped[str] = mapped_column(ascii_id(), primary_key=True)
    sid: Mapped[str] = mapped_column(
        ascii_id(),
        sa.ForeignKey("auth_session_families.sid", ondelete="CASCADE"),
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(ts6(), nullable=False)

    # also a backstop: a forked family (two successors of one head) cannot commit
    __table_args__ = (sa.UniqueConstraint("sid", "seq", name="uq_auth_session_members_sid_seq"),)


class UsedToken(Base):
    """Single-use tokens, nonces and dedupe markers: a duplicate key is a replay."""

    __tablename__ = "used_tokens"

    scope: Mapped[str] = mapped_column(ascii_id(32), primary_key=True)
    # sha256 hex of the raw token
    token: Mapped[str] = mapped_column(sa.CHAR(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(ts6(), nullable=False)

    __table_args__ = (sa.Index("ix_used_tokens_expires_at", "expires_at"),)


class Lease(Base):
    """Named leases (agent turn per org, scheduler tick)."""

    __tablename__ = "leases"

    name: Mapped[str] = mapped_column(ascii_id(), primary_key=True)
    holder: Mapped[str] = mapped_column(ascii_id(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(ts6(), nullable=False)
