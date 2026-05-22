"""Plans simulation sandbox router (spec 2026-05-22).

Mounted at ``/api/v1/scenarios``. Architect-locked:

- Internal name = ``scenarios``; user-facing label = "Plans". The
  router prefix uses the internal name. The UI never says
  "scenario".
- Per-user. Plans are private to the creator. Every query filters
  by ``org_id`` AND ``user_id``. A second user in the same org
  cannot read another member's scenarios.
- Horizon caps (120 for trip/purchase/custom, 480 for retirement)
  are enforced via ``schemas/scenario.py::validate_horizon`` on
  create, patch, and simulate paths.
- Sandboxing: ``simulate`` runs a no-DB engine over a frozen
  ``WorldState`` snapshot. It writes the projection blob back onto
  the ``Scenario`` row and commits — nothing else.
- ``compare`` endpoint is PR3 (out of scope for PR1).
- ``ai_enhanced`` engine raises NotImplementedError (PR4 stub).
"""
from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app._time import utcnow_naive
from app.database import get_db
from app.deps import get_current_user
from app.models.scenario import Scenario, ScenarioType
from app.models.user import User
from app.schemas.scenario import (
    ScenarioCreate,
    ScenarioResponse,
    ScenarioUpdate,
    SimulateRequest,
    validate_horizon,
)
from app.services.scenario_engine import (
    SimulationRequest,
    build_world_state,
    get_engine,
)


logger = structlog.stdlib.get_logger()

router = APIRouter(prefix="/api/v1/scenarios", tags=["scenarios"])


def _validate_horizon_or_422(scenario_type: str, horizon_months: int) -> None:
    """Wrap ``validate_horizon`` so router paths surface 422 (not 500).

    The schema-level ``model_validator`` already converts ValueError
    to 422 automatically. Router-level cap re-checks (on PATCH and on
    POST /simulate) bypass that path, so we wrap with an explicit
    HTTPException so the architect-locked cap surfaces uniformly as a
    422 across every entry point.
    """
    try:
        validate_horizon(scenario_type, horizon_months)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


async def _get_owned_scenario(
    db: AsyncSession, *, user: User, scenario_id: int
) -> Scenario:
    """Fetch a scenario AND assert the current user owns it.

    Architect-locked privacy: per-user scoping. Even a member of the
    same org cannot read another user's scenarios. We return 404 on
    cross-user attempts (NOT 403) so a probing client cannot enumerate
    existence by ownership boundary.
    """
    row = await db.get(Scenario, scenario_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Scenario not found")
    if row.user_id != user.id or row.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Scenario not found")
    return row


@router.get("", response_model=list[ScenarioResponse])
async def list_scenarios(
    include_archived: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the current user's scenarios.

    By default, archived (is_active=false) rows are hidden. Pass
    ``include_archived=true`` to include them.
    """
    stmt = select(Scenario).where(
        Scenario.org_id == current_user.org_id,
        Scenario.user_id == current_user.id,
    )
    if not include_archived:
        stmt = stmt.where(Scenario.is_active.is_(True))
    stmt = stmt.order_by(Scenario.created_at.desc())
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


@router.post("", response_model=ScenarioResponse, status_code=201)
async def create_scenario(
    body: ScenarioCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new scenario (Plan in UI terms) for the current user."""
    _validate_horizon_or_422(body.scenario_type.value, body.horizon_months)

    row = Scenario(
        org_id=current_user.org_id,
        user_id=current_user.id,
        name=body.name,
        scenario_type=body.scenario_type,
        params_json=body.params.model_dump(mode="json"),
        horizon_months=body.horizon_months,
        is_active=True,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.get("/{scenario_id}", response_model=ScenarioResponse)
async def get_scenario(
    scenario_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _get_owned_scenario(db, user=current_user, scenario_id=scenario_id)


@router.patch("/{scenario_id}", response_model=ScenarioResponse)
async def update_scenario(
    scenario_id: int,
    body: ScenarioUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Partial update: name, params, horizon_months, is_active.

    scenario_type is immutable post-create (the params blob's
    discriminator pins it).
    """
    row = await _get_owned_scenario(db, user=current_user, scenario_id=scenario_id)

    # Re-check horizon cap against the EXISTING row's scenario_type.
    if body.horizon_months is not None:
        _validate_horizon_or_422(row.scenario_type.value, body.horizon_months)
        row.horizon_months = body.horizon_months

    if body.name is not None:
        row.name = body.name

    if body.params is not None:
        if body.params.scenario_type != row.scenario_type.value:
            raise HTTPException(
                status_code=422,
                detail=(
                    "params scenario_type must match the existing "
                    "scenario_type on the row"
                ),
            )
        row.params_json = body.params.model_dump(mode="json")

    if body.is_active is not None:
        row.is_active = body.is_active

    await db.commit()
    await db.refresh(row)
    return row


@router.delete(
    "/{scenario_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_scenario(
    scenario_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Hard delete a scenario row. (Soft-delete is the ``is_active``
    flag set via PATCH; this endpoint is the hard remove.)
    """
    row = await _get_owned_scenario(db, user=current_user, scenario_id=scenario_id)
    await db.delete(row)
    await db.commit()


@router.post(
    "/{scenario_id}/simulate",
    response_model=ScenarioResponse,
)
async def simulate_scenario(
    scenario_id: int,
    body: SimulateRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Run the configured engine and cache the projection on the row.

    Body is optional; an empty body defaults to ``engine="analytic"``
    with no options and the row's stored horizon.

    The endpoint:
      1. Loads the scenario (404 on cross-user).
      2. Validates the request horizon (or the row's stored horizon)
         against the per-type cap.
      3. Builds a frozen WorldState snapshot via build_world_state
         (READ-ONLY).
      4. Runs the engine (no DB session inside the engine).
      5. Writes the projection back onto the scenario row and commits.

    Sandboxing: the engine has no path to mutate accounts /
    transactions / budgets / recurring / forecast_plans. Only
    Scenario.projection_json is written.
    """
    payload = body or SimulateRequest()
    row = await _get_owned_scenario(db, user=current_user, scenario_id=scenario_id)

    horizon = payload.horizon_months if payload.horizon_months is not None else row.horizon_months
    _validate_horizon_or_422(row.scenario_type.value, horizon)

    state = await build_world_state(
        db, org_id=current_user.org_id, user_id=current_user.id
    )

    engine = get_engine(payload.engine)
    sim_request = SimulationRequest(
        scenario=row,
        state=state,
        horizon_months=horizon,
        options=payload.options,
    )

    # The regression-overlay flag is a top-level field on
    # ``SimulateRequest`` (architect-locked spec), passed explicitly as
    # a kwarg to the engine. The engine never reads it from
    # ``req.options`` — there is exactly one source of truth.
    result = engine.simulate(
        sim_request,
        smooth_with_regression=payload.smooth_with_regression,
    )

    row.projection_json = result
    row.projection_engine = result.get("engine_name") or engine.name
    row.projection_computed_at = utcnow_naive()
    await db.commit()
    await db.refresh(row)
    return row
