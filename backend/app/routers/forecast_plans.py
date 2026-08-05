import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.schemas.forecast_plan import (
    BulkUpsertRequest,
    CopyPlanRequest,
    ForecastPlanItemCreate,
    ForecastPlanItemUpdate,
    ForecastPlanResponse,
)
from app.services import forecast_plan_service as svc
from app.services.feature_gate import Feature
from app.services.feature_gate import require_feature as require_product_area

# Router-level product gate (TBD-197). An org that switched Forecast off in
# Settings → Planning tools gets a hard 404 on all twelve handlers, not a 403:
# the surface is meant to be invisible, and the frontend already hides the nav
# entry and replaces the page with a one-line notice.
#
# Aliased as ``require_product_area`` for consistency with the AI routers,
# which carry BOTH gating systems: ``app.auth.feature_deps.require_feature``
# (AI entitlements, takes a ``str``, 403s) is a different function with the
# same name, and a plain import of either rebinds the other at import time.
router = APIRouter(
    prefix="/api/v1/forecast-plans",
    tags=["forecast-plans"],
    dependencies=[Depends(require_product_area(Feature.FORECAST))],
)


@router.get("", response_model=ForecastPlanResponse)
async def get_plan(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    period_start: datetime.date | None = Query(default=None),
):
    return await svc.get_or_create_plan(db, current_user.org_id, period_start=period_start)


@router.get("/current", response_model=ForecastPlanResponse | None)
async def get_current_plan(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    period_start: datetime.date | None = Query(default=None),
):
    """Read-only fetch — returns the plan for the period or null. Never
    creates a draft as a side effect. The Dashboard uses this so loading
    it doesn't auto-spawn empty plans in the DB."""
    return await svc.get_plan_for_period(db, current_user.org_id, period_start=period_start)


@router.post("/populate", response_model=ForecastPlanResponse)
async def populate_plan(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    period_start: datetime.date | None = Query(default=None),
):
    return await svc.populate_from_sources(db, current_user.org_id, period_start=period_start)


@router.post("/refresh-from-sources", response_model=ForecastPlanResponse)
async def refresh_from_sources_plan(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    period_start: datetime.date | None = Query(default=None),
):
    """Drop auto-generated items (source=recurring|history) and re-run
    populate. Manual items are preserved. Use this to pick up newly
    added recurring templates or transactions after the initial populate."""
    return await svc.refresh_from_sources(db, current_user.org_id, period_start=period_start)


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


@router.post("/{plan_id}/revert", response_model=ForecastPlanResponse)
async def revert_to_draft(
    plan_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await svc.revert_to_draft(db, current_user.org_id, plan_id)


@router.post("/{plan_id}/discard", response_model=ForecastPlanResponse)
async def discard_plan(
    plan_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await svc.discard_plan(db, current_user.org_id, plan_id)


@router.post("/copy", response_model=ForecastPlanResponse)
async def copy_plan(
    body: CopyPlanRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await svc.copy_from_period(
        db, current_user.org_id,
        target_period_start=body.target_period_start,
        source_period_start=body.source_period_start,
    )
