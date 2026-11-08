"""Goals inside a plan (TBD-601), mounted at ``/api/v1/scenarios/{scenario_id}/goals``.

Same rights as the plan (``plan_draft_service``): whoever sees the plan reads
its goals; its creator or an org owner or admin writes them (else 403); another
org's plan is a 404. Behind the same paid
gate as ``/api/v1/scenarios``. The checks live in ``plan_draft_service``.

Edit after start (TBD-602): on an active plan a write needs ``confirm=true``
and runs under the plan's row lock; a goal added or enabled there gets Start's
steps (``plan_lifecycle_service.start_goal``). A started plan's goal is not
deleted here: finish or delete the plan.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models.scenario import ScenarioStatus
from app.models.user import User
from app.schemas.plan_goal import PlanGoalCreate, PlanGoalResponse, PlanGoalUpdate
from app.services import plan_draft_service, plan_lifecycle_service
from app.services.exceptions import ConflictError, NotFoundError, ValidationError
from app.services.feature_gate import Feature, require_feature

# Edit after start is discouraged: the client confirms it (TBD-602).
CONFIRM = Query(default=False, description="confirm a change to a started plan")

router = APIRouter(
    prefix="/api/v1/scenarios/{scenario_id}/goals",
    tags=["scenarios"],
    dependencies=[Depends(require_feature(Feature.PLANS))],
)


@contextmanager
def http_errors() -> Iterator[None]:
    """The plan service's domain errors as the HTTP codes the scenarios
    router already uses (404, 403, 422, 409)."""
    try:
        yield
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except plan_draft_service.PlanEditDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from None
    except ConflictError as exc:
        # A coded conflict (a plan's claim already held, TBD-602) keeps its code.
        detail = {"code": exc.code, "message": exc.detail} if exc.code else exc.detail
        raise HTTPException(status_code=409, detail=detail) from None


async def _plan_for_write(db: AsyncSession, user: User, scenario_id: int):
    """The plan the user may change, locked: a concurrent Start or Finish
    either lands first, and the edit sees its status, or waits for the edit."""
    plan = await plan_draft_service.get_plan(db, user=user, plan_id=scenario_id, write=True)
    return await plan_lifecycle_service.lock(db, plan)


@router.get("", response_model=list[PlanGoalResponse])
async def list_goals(
    scenario_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    with http_errors():
        plan = await plan_draft_service.get_plan(db, user=current_user, plan_id=scenario_id)
    return await plan_draft_service.list_goals(db, plan=plan)


@router.post("", response_model=PlanGoalResponse, status_code=201)
async def create_goal(
    scenario_id: int,
    body: PlanGoalCreate,
    confirm: bool = CONFIRM,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    with http_errors():
        plan = await _plan_for_write(db, current_user, scenario_id)
        plan_draft_service.assert_editable(plan, confirm=confirm, changes={}, allowed=None)
        goal = await plan_draft_service.create_goal(db, plan=plan, fields=body.model_dump())
        if plan.status == ScenarioStatus.ACTIVE and goal.enabled:
            await plan_lifecycle_service.start_goal(db, plan=plan, goal=goal)
    await db.commit()
    await db.refresh(goal)
    return goal


@router.patch("/{goal_id}", response_model=PlanGoalResponse)
async def update_goal(
    scenario_id: int,
    goal_id: int,
    body: PlanGoalUpdate,
    confirm: bool = CONFIRM,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    with http_errors():
        plan = await _plan_for_write(db, current_user, scenario_id)
        goal = await plan_draft_service.get_goal(db, plan=plan, goal_id=goal_id)
        changes = body.changes()
        plan_draft_service.assert_editable(
            plan, confirm=confirm, changes=changes, allowed=plan_draft_service.STARTED_GOAL_FIELDS)
        was_enabled = goal.enabled
        await plan_draft_service.update_goal(db, plan=plan, goal=goal, changes=changes)
        if plan.status == ScenarioStatus.ACTIVE and goal.enabled and not was_enabled:
            await plan_lifecycle_service.start_goal(db, plan=plan, goal=goal)
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
        # Locked like POST and PATCH: the draft check runs on the locked row.
        plan = await _plan_for_write(db, current_user, scenario_id)
        goal = await plan_draft_service.get_goal(db, plan=plan, goal_id=goal_id)
        await plan_draft_service.delete_goal(db, plan=plan, goal=goal)
    await db.commit()
