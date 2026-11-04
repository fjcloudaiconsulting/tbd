"""Goals inside a plan (TBD-601), mounted at ``/api/v1/scenarios/{scenario_id}/goals``.

Org-scoped like the plans themselves: every member of the plan's org reads and
edits a draft's goals, and another org's plan is a 404. Behind the same paid
gate as ``/api/v1/scenarios``. The checks live in ``plan_draft_service``.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.schemas.plan_goal import PlanGoalCreate, PlanGoalResponse, PlanGoalUpdate
from app.services import plan_draft_service
from app.services.exceptions import ConflictError, NotFoundError, ValidationError
from app.services.feature_gate import Feature, require_feature

router = APIRouter(
    prefix="/api/v1/scenarios/{scenario_id}/goals",
    tags=["scenarios"],
    dependencies=[Depends(require_feature(Feature.PLANS))],
)


@contextmanager
def http_errors() -> Iterator[None]:
    """The plan service's domain errors as the HTTP codes the scenarios
    router already uses (404, 422, 409)."""
    try:
        yield
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from None
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from None


@router.get("", response_model=list[PlanGoalResponse])
async def list_goals(
    scenario_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    with http_errors():
        plan = await plan_draft_service.get_plan(db, org_id=current_user.org_id, plan_id=scenario_id)
    return await plan_draft_service.list_goals(db, plan=plan)


@router.post("", response_model=PlanGoalResponse, status_code=201)
async def create_goal(
    scenario_id: int,
    body: PlanGoalCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    with http_errors():
        plan = await plan_draft_service.get_plan(db, org_id=current_user.org_id, plan_id=scenario_id)
        goal = await plan_draft_service.create_goal(db, plan=plan, fields=body.model_dump())
    await db.commit()
    await db.refresh(goal)
    return goal


@router.patch("/{goal_id}", response_model=PlanGoalResponse)
async def update_goal(
    scenario_id: int,
    goal_id: int,
    body: PlanGoalUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    with http_errors():
        plan = await plan_draft_service.get_plan(db, org_id=current_user.org_id, plan_id=scenario_id)
        goal = await plan_draft_service.get_goal(db, plan=plan, goal_id=goal_id)
        await plan_draft_service.update_goal(db, plan=plan, goal=goal, changes=body.changes())
    await db.commit()
    await db.refresh(goal)
    return goal


@router.delete("/{goal_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_goal(
    scenario_id: int,
    goal_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    with http_errors():
        plan = await plan_draft_service.get_plan(db, org_id=current_user.org_id, plan_id=scenario_id)
        goal = await plan_draft_service.get_goal(db, plan=plan, goal_id=goal_id)
        await plan_draft_service.delete_goal(db, plan=plan, goal=goal)
    await db.commit()
