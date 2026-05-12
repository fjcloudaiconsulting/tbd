"""Admin system-usage analytics read API (L4.6).

Mounted at ``/api/v1/admin/analytics``. Gated by the platform
``analytics.view`` permission (superadmin short-circuits, fine-grained
roles via L4.8 layer in later without touching this file).

Read-only counts-only first slice. No third-party SDK; no per-user
event stream; no charts (follow-up). All numbers are derived from
existing tables (``audit_events`` for logins, ``transactions`` for
write/import volume, ``organizations`` for the org leaderboard and
dormancy list).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import require_permission
from app.database import get_db
from app.schemas.admin_analytics import AnalyticsResponse
from app.services import admin_analytics_service


router = APIRouter(prefix="/api/v1/admin/analytics", tags=["admin-analytics"])


@router.get(
    "",
    response_model=AnalyticsResponse,
    dependencies=[Depends(require_permission("analytics.view"))],
)
async def get_analytics(
    days: int = Query(default=30, ge=1, le=365),
    top_orgs_limit: int = Query(default=10, ge=1, le=100),
    dormant_threshold_days: int = Query(default=30, ge=0, le=365),
    db: AsyncSession = Depends(get_db),
) -> AnalyticsResponse:
    """System-usage analytics envelope. One round-trip per page render."""
    payload = await admin_analytics_service.build_analytics_payload(
        db,
        days=days,
        top_orgs_limit=top_orgs_limit,
        dormant_threshold_days=dormant_threshold_days,
    )
    return AnalyticsResponse.model_validate(payload)
