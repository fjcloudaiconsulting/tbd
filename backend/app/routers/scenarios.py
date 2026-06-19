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
- ``ai_enhanced`` engine routes through ``scenario_engine_ai.run_ai_simulation``
  which gate-checks ``ai.smart_plan`` + smart_plan routing and falls
  back to the analytic baseline on any failure (PR4).
"""
from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app._time import utcnow_naive
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models.account import Account
from app.models.category import Category
from app.models.recurring import RecurringTransaction
from app.models.scenario import Scenario, ScenarioType
from app.models.user import User
from app.schemas.scenario import (
    COMPARE_MAX_SCENARIOS,
    CompareRequest,
    CompareResponse,
    CompareProjection,
    ScenarioCreate,
    ScenarioResponse,
    ScenarioUpdate,
    SimulateRequest,
    validate_horizon,
)
from app.services.feature_gate import Feature, require_feature
from app.services.scenario_engine import (
    SimulationRequest,
    build_world_state,
    get_engine,
)
from app.services.scenario_engine_ai import run_ai_simulation


logger = structlog.stdlib.get_logger()

router = APIRouter(
    prefix="/api/v1/scenarios",
    tags=["scenarios"],
    dependencies=[Depends(require_feature(Feature.PLANS))],
)


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

    params_json = body.params.model_dump(mode="json")
    # PR3: custom events get cross-user FK + horizon-bound validation.
    if body.scenario_type == ScenarioType.CUSTOM:
        await _validate_custom_event_references(
            db,
            org_id=current_user.org_id,
            horizon_months=body.horizon_months,
            params=params_json,
        )

    row = Scenario(
        org_id=current_user.org_id,
        user_id=current_user.id,
        name=body.name,
        scenario_type=body.scenario_type,
        params_json=params_json,
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
        new_params = body.params.model_dump(mode="json")
        # PR3: custom events get cross-user FK + horizon-bound validation
        # on PATCH too. Use the row's CURRENT horizon (possibly just
        # updated above) so the bound check stays consistent.
        if row.scenario_type == ScenarioType.CUSTOM:
            await _validate_custom_event_references(
                db,
                org_id=current_user.org_id,
                horizon_months=row.horizon_months,
                params=new_params,
            )
        row.params_json = new_params

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
    request: Request,
    body: SimulateRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(
        get_session_factory
    ),
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
      4. Runs the engine. For ``engine="analytic"`` the sync analytic
         engine runs directly; for ``engine="ai_enhanced"`` we
         delegate to the async ``run_ai_simulation`` orchestrator,
         which calls the LLM via ``call_llm_structured``, adjusts
         assumptions, and re-runs the analytic engine. The
         orchestrator always falls back to the analytic baseline on
         any failure (gate closed, no routing, dispatch error,
         schema mismatch) so the frontend never crashes.
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

    if payload.engine == "ai_enhanced":
        # AI orchestrator: gate-check, call LLM, adjust assumptions,
        # re-run analytic, audit. Falls back to analytic baseline on
        # any failure mode (see scenario_engine_ai.run_ai_simulation).
        result = await run_ai_simulation(
            db,
            session_factory=session_factory,
            org_id=current_user.org_id,
            user_id=current_user.id,
            actor_email=current_user.email,
            scenario=row,
            state=state,
            horizon_months=horizon,
            options=payload.options,
            smooth_with_regression=payload.smooth_with_regression,
            request_id=getattr(request.state, "request_id", None),
            ip_address=getattr(request.client, "host", None),
        )
        engine_name = result.get("engine_name") or "ai_enhanced"
    else:
        # Analytic baseline (default). Sync engine; no DB session
        # passed in — the engine math has no path to the DB.
        engine = get_engine(payload.engine)
        sim_request = SimulationRequest(
            scenario=row,
            state=state,
            horizon_months=horizon,
            options=payload.options,
        )
        # The regression-overlay flag is a top-level field on
        # ``SimulateRequest`` (architect-locked spec), passed
        # explicitly as a kwarg to the engine. The engine never reads
        # it from ``req.options`` — there is exactly one source of
        # truth.
        result = engine.simulate(
            sim_request,
            smooth_with_regression=payload.smooth_with_regression,
        )
        engine_name = result.get("engine_name") or engine.name

    row.projection_json = result
    row.projection_engine = engine_name
    row.projection_computed_at = utcnow_naive()
    await db.commit()
    await db.refresh(row)
    return row


# ── PR3: comparison view ────────────────────────────────────────────────


@router.post("/compare", response_model=CompareResponse)
async def compare_scenarios(
    body: CompareRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Run the analytic engine on 1-3 scenarios at the SAME horizon
    and return the projections side-by-side.

    Architect-locked rules:

    - Max 3 scenarios (enforced by ``CompareRequest`` Field constraint).
    - Each scenario must belong to the current user; 404 on cross-user
      (matches the per-user privacy lock on the rest of the router).
    - The request's horizon is validated against EACH scenario's
      ``scenario_type`` cap. If any scenario rejects it, the whole
      compare 422s with the offending scenario id in the message.
    - Engine runs are sequential — sub-second each, max 3, so parallel
      gather adds complexity for no measurable win.
    - World state is built ONCE and reused across all engine runs (it
      depends only on org_id/user_id, not on the scenario).
    """
    # Load every scenario the request asked for, in the SAME order as
    # the request's scenario_ids list. Order preserved so the response
    # is positionally parallel.
    rows: list[Scenario] = []
    for sid in body.scenario_ids:
        row = await db.get(Scenario, sid)
        if row is None or row.user_id != current_user.id or row.org_id != current_user.org_id:
            raise HTTPException(
                status_code=404,
                detail=f"Scenario not found: {sid}",
            )
        rows.append(row)

    # Validate horizon against EACH scenario's per-type cap. The first
    # offender wins the 422 with its id in the detail.
    for row in rows:
        try:
            validate_horizon(row.scenario_type.value, body.horizon_months)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"scenario_id={row.id}: {exc}",
            )

    # Build the world state once (it's per-org, not per-scenario).
    state = await build_world_state(
        db, org_id=current_user.org_id, user_id=current_user.id
    )

    engine = get_engine("analytic")
    projections: list[CompareProjection] = []
    for row in rows:
        sim_request = SimulationRequest(
            scenario=row,
            state=state,
            horizon_months=body.horizon_months,
            options={},
        )
        result = engine.simulate(
            sim_request,
            smooth_with_regression=body.smooth_with_regression,
        )
        projections.append(
            CompareProjection(
                scenario_id=row.id,
                name=row.name,
                scenario_type=row.scenario_type,
                projection=result,
            )
        )

    return CompareResponse(projections=projections)


# ── Custom-event cross-user FK validation ───────────────────────────────


async def _validate_custom_event_references(
    db: AsyncSession,
    *,
    org_id: int,
    horizon_months: int,
    params: dict,
) -> None:
    """Pin custom events to the current user's org and the row's horizon.

    Raises HTTPException(422) with detail code ``event_invalid_reference``
    when an event references a recurring_id / account_id / category_id
    that does not belong to the current user's org, or when a month
    (or from_month / to_month) is < 0 or >= horizon_months.

    The engine iterates ``range(0, horizon_months)`` so valid month
    indices are ``[0, horizon_months - 1]``. An event at
    ``month == horizon_months`` (or ``from_month == horizon_months``,
    or ``to_month == horizon_months``) would silently never fire; we
    reject it as a misconfiguration instead.

    Schema-level validators already enforce ``from_month <= to_month``
    and ``>= 0`` on each event; this function is the cross-user
    leak gate plus the horizon-relative bound check.
    """
    events = params.get("events") or []
    for ev in events:
        ev_type = ev.get("type")
        # Bound month / from_month / to_month against horizon. The engine
        # iterates range(0, horizon_months), so the last valid month index
        # is horizon_months - 1. Anything >= horizon_months silently does
        # nothing — reject it.
        for field_name in ("month", "from_month", "to_month"):
            val = ev.get(field_name)
            if val is None:
                continue
            try:
                ival = int(val)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "event_invalid_reference",
                        "message": f"custom event {field_name} must be an integer",
                    },
                )
            if ival >= horizon_months:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "event_invalid_reference",
                        "message": (
                            f"custom event {field_name}={ival} is outside the "
                            f"simulated range [0, horizon_months - 1] "
                            f"(horizon_months={horizon_months})"
                        ),
                    },
                )

        # FK leak prevention: each referenced row must belong to the
        # current user's org.
        if ev_type == "recurring_on":
            rid = ev.get("recurring_id")
            if rid is None:
                continue
            rec = await db.get(RecurringTransaction, int(rid))
            if rec is None or rec.org_id != org_id:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "event_invalid_reference",
                        "message": f"recurring_id {rid} not found for this org",
                    },
                )
        if ev_type in ("one_off_income", "one_off_expense"):
            aid = ev.get("account_id")
            if aid is not None:
                acc = await db.get(Account, int(aid))
                if acc is None or acc.org_id != org_id:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "code": "event_invalid_reference",
                            "message": f"account_id {aid} not found for this org",
                        },
                    )
            cid = ev.get("category_id")
            if cid is not None:
                cat = await db.get(Category, int(cid))
                if cat is None or cat.org_id != org_id:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "code": "event_invalid_reference",
                            "message": f"category_id {cid} not found for this org",
                        },
                    )
        if ev_type == "expense_off":
            cat_ids = ev.get("category_ids") or []
            for cid in cat_ids:
                cat = await db.get(Category, int(cid))
                if cat is None or cat.org_id != org_id:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "code": "event_invalid_reference",
                            "message": f"category_id {cid} not found for this org",
                        },
                    )
