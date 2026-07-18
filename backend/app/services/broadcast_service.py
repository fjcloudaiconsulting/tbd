"""Superadmin email broadcast service (spec ``2026-07-18-admin-email-broadcast-design.md``).

This module grows across Tasks 2-4 of the implementation plan. This
slice (Task 2) only adds segment resolution:

- ``count_segment`` — live COUNT for a segment, used both for the
  draft's advertised ``recipient_count`` and for the send-time
  recipient-cap check.
- ``iter_segment_users`` — the rows materialization will snapshot into
  ``email_broadcast_recipients`` in a later task.

``active_verified`` is, per Ruling 10, the only segment v1 accepts —
any other value is an app-level ``ValueError`` before it ever reaches
the DB (there is no promotional/re-engagement audience without an
unsubscribe + suppression mechanism first).
"""
from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.email_broadcast import SEGMENT_ACTIVE_VERIFIED
from app.models.user import User


def _require_known_segment(segment: str) -> None:
    if segment != SEGMENT_ACTIVE_VERIFIED:
        raise ValueError(f"unknown broadcast segment: {segment!r}")


async def count_segment(db: AsyncSession, segment: str) -> int:
    """Return the live count of users targeted by ``segment``.

    Raises ``ValueError`` for any segment other than
    ``SEGMENT_ACTIVE_VERIFIED`` (Ruling 10).
    """
    _require_known_segment(segment)
    result = await db.execute(
        select(func.count())
        .select_from(User)
        .where(User.is_active.is_(True), User.email_verified.is_(True))
    )
    return int(result.scalar_one())


async def iter_segment_users(
    db: AsyncSession, segment: str
) -> Sequence[tuple[int, str, str | None]]:
    """Return ``(user_id, email, first_name)`` for every user in ``segment``.

    Materialization (a later task) snapshots these tuples into
    ``EmailBroadcastRecipient`` rows. Raises ``ValueError`` for any
    segment other than ``SEGMENT_ACTIVE_VERIFIED`` (Ruling 10).
    """
    _require_known_segment(segment)
    result = await db.execute(
        select(User.id, User.email, User.first_name)
        .where(User.is_active.is_(True), User.email_verified.is_(True))
        .order_by(User.id)
    )
    return [tuple(row) for row in result.all()]
