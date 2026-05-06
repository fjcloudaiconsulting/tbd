"""Per-org exclusive lease for in-flight org-data resets.

A single row per org while a reset is running. The endpoint
(``routers/org_data.py``) acquires the lock before calling
``reset_org_data`` and releases it in ``finally``; a stale-lock TTL
in the service layer auto-recovers from crashed workers.

Without this guard, two concurrent reset POSTs could interleave —
because the seed-defaults logic relies on app-level idempotence
(no DB-level UNIQUE on system slugs), an interleave window can
duplicate the post-wipe defaults. Logged as residual risk on
PR #134; this table closes it.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Integer, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class OrgDataResetLock(Base):
    __tablename__ = "org_data_reset_locks"

    org_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    acquired_by_user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    acquired_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # UUID4 hex string. Generated fresh on every acquire (whether
    # an INSERT into an empty row or a stale-takeover UPDATE). The
    # release path fences on `WHERE org_id = :id AND lease_token =
    # :token`, so the original caller of a since-stale-taken-over
    # lock cannot accidentally delete the successor's fresh lease.
    lease_token: Mapped[str] = mapped_column(String(36), nullable=False)
