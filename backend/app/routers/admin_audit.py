"""Admin audit-log read API (L4.7).

Mounted at ``/api/v1/admin/audit``. Gated by the platform
``audit.view`` permission (superadmin short-circuits, fine-grained
roles via L4.8 layer in later without touching this file).

Read-only. Writes happen as a side effect inside admin and tenant
routers via ``audit_service.record_audit_event`` so the events the
log captures are the same events the structlog stream emits.
"""
from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import require_permission
from app.database import get_db
from app.schemas.audit import AuditEventListResponse, AuditEventResponse
from app.services import audit_service


router = APIRouter(prefix="/api/v1/admin/audit", tags=["admin-audit"])


@router.get(
    "",
    response_model=AuditEventListResponse,
    dependencies=[Depends(require_permission("audit.view"))],
)
async def list_audit_events(
    actor_user_id: int | None = Query(default=None, ge=1),
    target_org_id: int | None = Query(default=None, ge=1),
    event_type: str | None = Query(default=None, max_length=80),
    outcome: str | None = Query(default=None, max_length=16),
    from_dt: datetime.datetime | None = Query(default=None),
    to_dt: datetime.datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> AuditEventListResponse:
    rows, total = await audit_service.list_audit_events(
        db,
        actor_user_id=actor_user_id,
        target_org_id=target_org_id,
        event_type=event_type,
        outcome=outcome,
        from_dt=from_dt,
        to_dt=to_dt,
        limit=limit,
        offset=offset,
    )
    return AuditEventListResponse(
        items=[AuditEventResponse.model_validate(r) for r in rows],
        total=total,
    )
