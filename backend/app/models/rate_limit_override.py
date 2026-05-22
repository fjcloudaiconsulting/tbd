"""Per-org / per-user rate limit override (L4.10).

A row attaches either to an ``org_id`` OR a ``user_id`` (never both,
never neither). The "one scope per row" invariant is enforced by the
service layer (single write surface) rather than a CHECK constraint
so the SQLite-backed unit tests behave identically to MySQL 8.

The pair ``(max_requests, period_seconds)`` maps to slowapi's
``"N/period"`` string at resolve time. Period is stored in seconds
(not a textual unit) so the admin UI can sort / range-filter on it.

Lookup pattern: a request that wants to know the override for
endpoint ``E`` consults user override first, then org override. Both
queries are answered in one index seek via the composite indexes
defined in migration 059.

``expires_at`` is service-honoured: a row with ``expires_at <= now()``
is treated as absent. Cache invalidation TTL (60 s) bounds the worst-
case staleness window without forcing a separate sweeper job.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class RateLimitOverride(Base):
    __tablename__ = "rate_limit_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
    )
    user_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )
    endpoint_pattern: Mapped[str] = mapped_column(String(80), nullable=False)
    max_requests: Mapped[int] = mapped_column(Integer, nullable=False)
    period_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_by_user_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
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
