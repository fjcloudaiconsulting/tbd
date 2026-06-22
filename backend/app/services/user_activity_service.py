"""Throttled per-user activity stamp for the founding-members program.

Writes ``users.last_active_at`` at most once per throttle window, on an
INDEPENDENT session (same pattern as ``record_audit_event``) so the
request's own transaction is never touched and an auth request can never
be broken by a stamp failure. Activity is tracked NOW; the
"lose founder status after 30 days idle" rule ships later with payments.
"""
from __future__ import annotations

from datetime import datetime, timezone

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.user import User

logger = structlog.stdlib.get_logger(__name__)


async def maybe_stamp_last_active(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: int,
    current: datetime | None,
) -> None:
    """Stamp ``last_active_at = now`` for ``user_id`` if the stored value is
    stale (``None`` or older than the throttle window). No-op when fresh.

    Best-effort: opens its own session and swallows any error — a failed
    stamp must never break the authenticated request that triggered it.
    """
    now = datetime.now(timezone.utc)
    if current is not None:
        # Stored value may be tz-naive (MySQL DATETIME). Treat naive as UTC.
        cur = current if current.tzinfo else current.replace(tzinfo=timezone.utc)
        if (now - cur).total_seconds() < settings.last_active_stamp_throttle_seconds:
            return
    try:
        async with session_factory() as session:
            await session.execute(
                update(User).where(User.id == user_id).values(last_active_at=now)
            )
            await session.commit()
    except Exception:  # noqa: BLE001 — never break auth on a stamp failure
        logger.warning("user_activity.stamp_failed", user_id=user_id)
