"""Admin surface — superadmin-only operator dashboards.

L4.2 ships the home page: backend `GET /api/v1/admin/dashboard`
feeding the frontend route `/admin`. Subsequent L4.x PRs (org
management, user management, audit log, etc.) add siblings under
this router's `/api/v1/admin/*` prefix.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import require_permission
from app.database import get_db
from app.services.admin_dashboard_service import build_dashboard_payload

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.get(
    "/dashboard",
    dependencies=[Depends(require_permission("admin.view"))],
)
async def get_dashboard(db: AsyncSession = Depends(get_db)) -> dict:
    """KPIs + system-health snapshot for the `/admin` home page."""
    return await build_dashboard_payload(db)
