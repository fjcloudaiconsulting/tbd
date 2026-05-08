import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.schemas.forecast import AccountBalanceForecastResponse
from app.services import account_balance_forecast_service, forecast_service

router = APIRouter(prefix="/api/v1/forecast", tags=["forecast"])


@router.get("")
async def get_forecast(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    period_start: datetime.date | None = Query(default=None),
):
    return await forecast_service.compute_forecast(
        db, current_user.org_id, period_start=period_start
    )


@router.get("/account-balances", response_model=AccountBalanceForecastResponse)
async def get_account_balance_forecast(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    period_start: datetime.date | None = Query(default=None),
):
    """Per-account expected month-end balance for a billing period.

    Dashboard-only view: balance + pending delta in the period. Excludes
    settled rows (already in stored balance) and manual adjustments
    (settled-only today). Includes pending transfer legs because they
    move per-account balances even though they aren't reportable.
    """
    return await account_balance_forecast_service.compute_account_balance_forecast(
        db, current_user.org_id, period_start=period_start
    )
