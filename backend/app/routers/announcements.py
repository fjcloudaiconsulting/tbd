"""Customer-facing announcements router (spec 2026-05-21).

Mounted at ``/api/v1/announcements``. Two endpoints:

- ``GET /announcements`` — list active, in-window announcements that
  the current user hasn't dismissed (maintenance severity is always
  shown regardless of dismissal). Severity-then-newest ordering.
- ``POST /announcements/{id}/dismiss`` — record a dismissal for the
  current user. Idempotent: a double POST is still ``204``. Returns
  ``400 code=announcement_not_dismissible`` on maintenance severity
  (the FE renders no dismiss button there either, but the backend
  is the authoritative gate per the architect's both-layers rule).
"""
from __future__ import annotations

from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import case, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app._time import utcnow_naive
from app.database import get_db
from app.deps import get_current_user
from app.models.announcement import (
    Announcement,
    AnnouncementSeverity,
    UserDismissedAnnouncement,
)
from app.models.user import User
from app.schemas.announcement import AnnouncementResponse


logger = structlog.stdlib.get_logger()

router = APIRouter(prefix="/api/v1/announcements", tags=["announcements"])


# Severity sort priority (lower = on top). Drives the ORDER BY in the
# active-list query and the AppShell stacking order on the FE. Stable
# under future severity additions: a new severity gets a new ordinal
# and slots itself between existing ones explicitly.
_SEVERITY_ORDER: dict[AnnouncementSeverity, int] = {
    AnnouncementSeverity.MAINTENANCE: 0,
    AnnouncementSeverity.PROMO: 1,
    AnnouncementSeverity.INFO: 2,
}


@router.get("", response_model=list[AnnouncementResponse])
async def list_active_announcements(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return active + in-window announcements visible to this user."""
    now = utcnow_naive()

    # Pre-fetch this user's dismissed announcement ids so the filter is
    # a small IN-list rather than a per-row NOT EXISTS subquery. For
    # typical dismissal counts (single-digit) this is cheaper and keeps
    # the read query portable across MySQL + SQLite test backends.
    dismissed_rows = await db.execute(
        select(UserDismissedAnnouncement.announcement_id).where(
            UserDismissedAnnouncement.user_id == current_user.id
        )
    )
    dismissed_ids: set[int] = {row[0] for row in dismissed_rows.all()}

    severity_priority = case(
        {sev: rank for sev, rank in _SEVERITY_ORDER.items()},
        value=Announcement.severity,
        else_=99,
    )

    stmt = (
        select(Announcement)
        .where(Announcement.is_active.is_(True))
        .where(
            (Announcement.start_at.is_(None)) | (Announcement.start_at <= now)
        )
        .where(
            (Announcement.end_at.is_(None)) | (Announcement.end_at > now)
        )
        .order_by(severity_priority.asc(), Announcement.created_at.desc())
    )
    result = await db.execute(stmt)
    rows = list(result.scalars().all())

    # Visibility: maintenance is force-shown, everything else is hidden
    # once dismissed. Filtering in Python is fine — the candidate set
    # is already small (active + in-window).
    visible = [
        row
        for row in rows
        if row.severity == AnnouncementSeverity.MAINTENANCE
        or row.id not in dismissed_ids
    ]
    return visible


@router.post(
    "/{announcement_id}/dismiss",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def dismiss_announcement(
    announcement_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Record a dismissal for the current user. Idempotent.

    ``maintenance`` severity rejects with 400 + structured code so the
    operator-facing failure mode matches the FE's no-button rule.
    Unknown announcement id → 404.
    """
    row = await db.get(Announcement, announcement_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Announcement not found",
        )
    if row.severity == AnnouncementSeverity.MAINTENANCE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "announcement_not_dismissible",
                "message": "Maintenance announcements cannot be dismissed.",
            },
        )

    # Idempotent upsert: the composite PK makes a re-dismiss a no-op
    # at the DB layer. We INSERT ... ON DUPLICATE KEY UPDATE on MySQL
    # and fall back to a select-then-insert path on other dialects
    # (SQLite under pytest).
    dialect = db.bind.dialect.name if db.bind is not None else "sqlite"
    if dialect == "mysql":
        # ON DUPLICATE KEY UPDATE is the cleanest MySQL idiom; we
        # update the dismissed_at column to its existing value so the
        # write is a no-op when the row is already present.
        stmt = mysql_insert(UserDismissedAnnouncement).values(
            user_id=current_user.id,
            announcement_id=announcement_id,
        )
        stmt = stmt.on_duplicate_key_update(
            dismissed_at=UserDismissedAnnouncement.dismissed_at
        )
        await db.execute(stmt)
    else:
        existing = await db.get(
            UserDismissedAnnouncement, (current_user.id, announcement_id)
        )
        if existing is None:
            db.add(
                UserDismissedAnnouncement(
                    user_id=current_user.id,
                    announcement_id=announcement_id,
                )
            )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
