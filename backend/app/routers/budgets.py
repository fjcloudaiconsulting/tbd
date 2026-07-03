import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.schemas.budget import (
    BudgetCreate,
    BudgetResponse,
    BudgetTransfer,
    BudgetUpdate,
    CopyBudgetsRequest,
)
from app.services import budget_service as svc

router = APIRouter(prefix="/api/v1/budgets", tags=["budgets"])


@router.get("", response_model=list[BudgetResponse])
async def list_budgets(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    period_start: datetime.date | None = Query(default=None),
):
    return await svc.list_budgets(db, current_user.org_id, period_start=period_start)


@router.post("", response_model=BudgetResponse, status_code=201)
async def create_budget(
    body: BudgetCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    period_start: datetime.date | None = Query(default=None),
):
    return await svc.create_budget(db, current_user.org_id, body, period_start=period_start)


@router.put("/{budget_id}", response_model=BudgetResponse)
async def update_budget(
    budget_id: int,
    body: BudgetUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await svc.update_budget(db, current_user.org_id, budget_id, body)


@router.post("/from-forecast", response_model=list[BudgetResponse])
async def create_from_forecast(
    period_start: datetime.date | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Seed a period's budgets from its forecast plan.

    Copies expense forecast items into Budget rows for the same period.
    ``period_start`` defaults to the current period; pass a future
    period's start to seed the next period from its plan. Skips
    categories that already have a budget — idempotent on repeat
    calls. Returns the full budget list for that period."""
    return await svc.create_budgets_from_forecast(
        db, current_user.org_id, period_start=period_start
    )


@router.post("/copy-from-period", response_model=list[BudgetResponse])
async def copy_from_period(
    body: CopyBudgetsRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Seed a target period's budgets from a source period's amounts.

    ``target_period_start`` defaults to the current period; pass a future
    period's start to copy the current period forward. Skips categories
    already budgeted in the target — idempotent. Returns the target
    period's full budget list."""
    return await svc.copy_budgets_from_period(
        db,
        current_user.org_id,
        source_period_start=body.source_period_start,
        target_period_start=body.target_period_start,
    )


@router.post("/transfer", response_model=list[BudgetResponse])
async def transfer_budget(
    body: BudgetTransfer,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await svc.transfer_budget(
        db, current_user.org_id,
        from_budget_id=body.from_budget_id,
        to_category_id=body.to_category_id,
        amount=body.amount,
    )


@router.delete("/{budget_id}", status_code=204)
async def delete_budget(
    budget_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await svc.delete_budget(db, current_user.org_id, budget_id)
