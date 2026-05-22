"""Superadmin-only CRUD for operator-authored announcements.

Mounted at ``/api/v1/admin/announcements``. Every endpoint requires
``is_superadmin=True`` per the architect's 2026-05-21 resolution.
Announcements are global content (not org-scoped), so the right
ceiling matches the existing superadmin gate rather than the
role-based ``orgs.manage`` style.

Every mutating endpoint writes an ``audit_events`` row via
``record_audit_event`` on an independent session — the audit trail
must survive a business-layer rollback (mirrors the
``feedback_service.submit_feedback`` pattern).
"""
from __future__ import annotations

from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models.announcement import Announcement, AnnouncementSeverity
from app.models.user import User
from app.rate_limit import get_client_ip
from app.schemas.announcement import (
    AnnouncementAdminResponse,
    AnnouncementCreate,
    AnnouncementUpdate,
)
from app.services import audit_service


logger = structlog.stdlib.get_logger()

router = APIRouter(prefix="/api/v1/admin/announcements", tags=["admin-announcements"])


def _request_id() -> Optional[str]:
    return structlog.contextvars.get_contextvars().get("request_id")


async def require_superadmin(
    current_user: User = Depends(get_current_user),
) -> User:
    """Dependency gate — 403 if the caller isn't a superadmin.

    Announcements are global content, gated above the role system,
    so we don't reach into ``require_permission`` here (no permission
    key exists for "any superadmin write"). Locking on ``is_superadmin``
    directly keeps the surface obvious and matches the spec.
    """
    if not current_user.is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden",
        )
    return current_user


def _audit_detail(row: Announcement) -> dict:
    return {
        "announcement_id": row.id,
        "severity": row.severity.value if hasattr(row.severity, "value") else str(row.severity),
        "is_active": row.is_active,
        "title_length": len(row.title or ""),
    }


@router.get("", response_model=list[AnnouncementAdminResponse])
async def list_announcements(
    _current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    """List every announcement regardless of active state or window.

    The admin UI filters client-side; backend returns the full set
    ordered newest first.
    """
    result = await db.execute(
        select(Announcement).order_by(Announcement.created_at.desc(), Announcement.id.desc())
    )
    return list(result.scalars().all())


@router.post(
    "",
    response_model=AnnouncementAdminResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_announcement(
    body: AnnouncementCreate,
    request: Request,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Create an announcement. Writes a ``system.announcement.created``
    audit event on commit.
    """
    actor_user_id = current_user.id
    actor_email = current_user.email

    row = Announcement(
        title=body.title,
        body=body.body,
        severity=body.severity,
        is_active=body.is_active,
        start_at=body.start_at,
        end_at=body.end_at,
        created_by_user_id=actor_user_id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    await audit_service.record_audit_event(
        session_factory,
        event_type="system.announcement.created",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=None,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail=_audit_detail(row),
    )

    await logger.ainfo(
        "system.announcement.created",
        announcement_id=row.id,
        severity=row.severity.value,
        is_active=row.is_active,
    )
    return row


@router.patch("/{announcement_id}", response_model=AnnouncementAdminResponse)
async def update_announcement(
    announcement_id: int,
    body: AnnouncementUpdate,
    request: Request,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Partial update. Re-checks the ``end_at > start_at`` invariant
    against the merged (existing + patch) state — the per-payload
    Pydantic validator only sees what the request carries.
    """
    actor_user_id = current_user.id
    actor_email = current_user.email

    row = await db.get(Announcement, announcement_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Announcement not found",
        )

    patch = body.model_dump(exclude_unset=True)
    merged_start = patch.get("start_at", row.start_at)
    merged_end = patch.get("end_at", row.end_at)
    if merged_start is not None and merged_end is not None and merged_end <= merged_start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="end_at must be strictly after start_at",
        )

    for field, value in patch.items():
        setattr(row, field, value)

    await db.commit()
    await db.refresh(row)

    await audit_service.record_audit_event(
        session_factory,
        event_type="system.announcement.updated",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=None,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail={**_audit_detail(row), "patched_fields": sorted(patch.keys())},
    )

    await logger.ainfo(
        "system.announcement.updated",
        announcement_id=row.id,
        patched_fields=sorted(patch.keys()),
    )
    return row


@router.delete(
    "/{announcement_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_announcement(
    announcement_id: int,
    request: Request,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Hard delete. Cascades to ``user_dismissed_announcements`` via FK."""
    actor_user_id = current_user.id
    actor_email = current_user.email

    row = await db.get(Announcement, announcement_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Announcement not found",
        )

    # Snapshot for the audit detail before the row is gone.
    audit_blob = _audit_detail(row)

    await db.delete(row)
    await db.commit()

    await audit_service.record_audit_event(
        session_factory,
        event_type="system.announcement.deleted",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=None,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail=audit_blob,
    )

    await logger.ainfo(
        "system.announcement.deleted",
        announcement_id=announcement_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
