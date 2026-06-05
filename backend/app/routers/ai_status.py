"""Authenticated endpoint: GET /api/v1/ai/status.

Returns per-feature {entitled, configured} for the current user's org.
Not in the public allowlist — requires a valid access token.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.schemas.ai_status import AIStatusResponse
from app.services.ai_status_service import get_ai_feature_status

router = APIRouter(prefix="/api/v1/ai", tags=["ai"])


@router.get("/status", response_model=AIStatusResponse)
async def ai_status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await get_ai_feature_status(db, org_id=current_user.org_id)
