import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.schemas.forecast_plan import (
    BulkUpsertRequest,
    ForecastPlanItemCreate,
    ForecastPlanItemUpdate,
    ForecastPlanResponse,
)
from app.services import forecast_plan_service as svc

router = APIRouter(prefix="/api/v1/forecast-plans", tags=["forecast-plans"])


@router.get("", response_model=ForecastPlanResponse)
async def get_plan(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    period_start: datetime.date | None = Query(default=None),
):
    return await svc.get_or_create_plan(db, current_user.org_id, period_start=period_start)


@router.post("/populate", response_model=ForecastPlanResponse)
async def populate_plan(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    period_start: datetime.date | None = Query(default=None),
):
    return await svc.populate_from_sources(db, current_user.org_id, period_start=period_start)


@router.post("/{plan_id}/items", response_model=ForecastPlanResponse, status_code=201)
async def add_item(
    plan_id: int,
    body: ForecastPlanItemCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await svc.upsert_item(db, current_user.org_id, plan_id, body)


@router.post("/{plan_id}/items/bulk", response_model=ForecastPlanResponse)
async def bulk_upsert_items(
    plan_id: int,
    body: BulkUpsertRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await svc.bulk_upsert(db, current_user.org_id, plan_id, body)


@router.put("/{plan_id}/items/{item_id}", response_model=ForecastPlanResponse)
async def update_item(
    plan_id: int,
    item_id: int,
    body: ForecastPlanItemUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await svc.update_item(db, current_user.org_id, plan_id, item_id, body)


@router.delete("/{plan_id}/items/{item_id}", response_model=ForecastPlanResponse)
async def delete_item(
    plan_id: int,
    item_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await svc.delete_item(db, current_user.org_id, plan_id, item_id)


@router.post("/{plan_id}/activate", response_model=ForecastPlanResponse)
async def activate_plan(
    plan_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await svc.activate_plan(db, current_user.org_id, plan_id)


@router.post("/copy", response_model=ForecastPlanResponse)
async def copy_plan(
    source_period_start: datetime.date = Query(),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    period_start: datetime.date | None = Query(default=None),
):
    return await svc.copy_from_period(
        db, current_user.org_id,
        target_period_start=period_start,
        source_period_start=source_period_start,
    )
