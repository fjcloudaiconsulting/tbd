import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.services import forecast_service as svc

router = APIRouter(prefix="/api/v1/forecast", tags=["forecast"])


@router.get("")
async def get_forecast(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    period_start: datetime.date | None = Query(default=None),
):
    return await svc.compute_forecast(db, current_user.org_id, period_start=period_start)
