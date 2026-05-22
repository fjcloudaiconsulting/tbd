"""Superadmin-only CRUD for rate-limit overrides (L4.10).

Mounted at ``/api/v1/admin/rate-limit-overrides``. Every endpoint
requires ``is_superadmin=True`` — overrides change the per-request
budget for an entire org or user, so the right ceiling matches the
existing announcement / global-platform gates.

Every mutating endpoint writes an ``audit_events`` row via
``record_audit_event`` on an independent session. The audit row
carries the override id, the resolved scope (``org_id`` /
``user_id``), the endpoint pattern, and the resulting ``max`` /
``period`` so an operator reviewing the audit log later can
reconstruct exactly what changed without joining back to the
override row (which may have been deleted).
"""
from __future__ import annotations

from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models.user import User
from app.rate_limit import get_client_ip
from app.rate_limit_endpoint_catalogue import (
    PRE_AUTH_PATTERNS,
    sorted_patterns,
)
from app.schemas.rate_limit_override import (
    RateLimitOverrideCreate,
    RateLimitOverrideResponse,
    RateLimitOverrideUpdate,
)
from app.services import audit_service
from app.services import rate_limit_overrides_service as svc


logger = structlog.stdlib.get_logger()

router = APIRouter(
    prefix="/api/v1/admin/rate-limit-overrides",
    tags=["admin-rate-limit-overrides"],
)


def _request_id() -> Optional[str]:
    return structlog.contextvars.get_contextvars().get("request_id")


async def require_superadmin(
    current_user: User = Depends(get_current_user),
) -> User:
    if not current_user.is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden",
        )
    return current_user


def _audit_detail(row) -> dict:
    return {
        "override_id": row.id,
        "scope": "user" if row.user_id is not None else "org",
        "org_id": row.org_id,
        "user_id": row.user_id,
        "endpoint_pattern": row.endpoint_pattern,
        "max_requests": row.max_requests,
        "period_seconds": row.period_seconds,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }


@router.get("/endpoint-catalogue", response_model=dict)
async def get_endpoint_catalogue(
    _current_user: User = Depends(require_superadmin),
):
    """Return the catalogue of supported endpoint patterns.

    Drives the admin UI's pattern dropdown so an operator can only
    pick a string the codebase actually has a ``@limiter.limit``
    decorator for. The response also flags pre-auth patterns; the UI
    surfaces a warning when one of those is selected (overrides on
    pre-auth routes are accepted but the resolver short-circuits to
    the static default).
    """
    return {
        "patterns": sorted_patterns(),
        "pre_auth_patterns": sorted(PRE_AUTH_PATTERNS),
    }


@router.get("", response_model=dict)
async def list_overrides_endpoint(
    org_id: Optional[int] = Query(default=None, ge=1),
    user_id: Optional[int] = Query(default=None, ge=1),
    endpoint_pattern: Optional[str] = Query(default=None, max_length=80),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    """List overrides with optional filters. Returns a paginated
    ``{items, total}`` envelope matching the existing
    ``AuditEventListResponse`` shape so the admin table can reuse the
    same pagination component.
    """
    rows, total = await svc.list_overrides(
        db,
        org_id=org_id,
        user_id=user_id,
        endpoint_pattern=endpoint_pattern,
        limit=limit,
        offset=offset,
    )
    return {
        "items": [RateLimitOverrideResponse.model_validate(r) for r in rows],
        "total": total,
    }


@router.post(
    "",
    response_model=RateLimitOverrideResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_override_endpoint(
    body: RateLimitOverrideCreate,
    request: Request,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Create a new override. Writes an ``admin.rate_limit.created``
    audit event on commit.
    """
    actor_user_id = current_user.id
    actor_email = current_user.email

    row = await svc.create_override(
        db,
        org_id=body.org_id,
        user_id=body.user_id,
        endpoint_pattern=body.endpoint_pattern,
        max_requests=body.max_requests,
        period_seconds=body.period_seconds,
        expires_at=body.expires_at,
        created_by_user_id=actor_user_id,
        note=body.note,
    )

    await audit_service.record_audit_event(
        session_factory,
        event_type="admin.rate_limit.created",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=row.org_id,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail=_audit_detail(row),
    )

    await logger.ainfo(
        "admin.rate_limit.created",
        override_id=row.id,
        org_id=row.org_id,
        user_id=row.user_id,
        endpoint_pattern=row.endpoint_pattern,
    )
    return row


@router.patch(
    "/{override_id}",
    response_model=RateLimitOverrideResponse,
)
async def update_override_endpoint(
    override_id: int,
    body: RateLimitOverrideUpdate,
    request: Request,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Partial update. Scope (``org_id`` / ``user_id``) is immutable
    once written; the schema does not surface those keys.
    """
    actor_user_id = current_user.id
    actor_email = current_user.email

    row = await svc.get_by_id(db, override_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Override not found",
        )

    patch = body.model_dump(exclude_unset=True)
    if not patch:
        return row

    row = await svc.update_override(db, row=row, patch=patch)

    await audit_service.record_audit_event(
        session_factory,
        event_type="admin.rate_limit.updated",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=row.org_id,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail={
            **_audit_detail(row),
            "patched_fields": sorted(patch.keys()),
        },
    )

    await logger.ainfo(
        "admin.rate_limit.updated",
        override_id=row.id,
        patched_fields=sorted(patch.keys()),
    )
    return row


@router.delete(
    "/{override_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_override_endpoint(
    override_id: int,
    request: Request,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    actor_user_id = current_user.id
    actor_email = current_user.email

    row = await svc.get_by_id(db, override_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Override not found",
        )

    audit_blob = _audit_detail(row)
    target_org_id = row.org_id

    await svc.delete_override(db, row=row)

    await audit_service.record_audit_event(
        session_factory,
        event_type="admin.rate_limit.deleted",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=target_org_id,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail=audit_blob,
    )

    await logger.ainfo(
        "admin.rate_limit.deleted",
        override_id=override_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
