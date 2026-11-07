"""Plans simulation sandbox router (spec 2026-05-22).

Mounted at ``/api/v1/scenarios``. Architect-locked:

- Internal name = ``scenarios``; user-facing label = "Plans". The
  router prefix uses the internal name. The UI never says
  "scenario".
- Visibility (TBD-601, the reports precedent): a legacy scenario stays
  private to its creator (``user_id``); a plan (a row with plan fields)
  is visible to every member of its org, and its creator or an org
  OWNER / ADMIN changes it (else 403). Another org's row, or another
  member's legacy one, is a 404. Simulate and compare follow
  visibility. The rules and the plan-field and goal checks
  (``plan_goals.py``) are in ``plan_draft_service``.
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
- Lifecycle (TBD-602, ``plan_lifecycle_service``): ``start`` and ``finish``,
  ``DELETE`` in any state, and ``progress``. Same rights as an edit. Editing a
  started plan needs ``confirm=true``; a finished plan is read-only.
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app._time import utcnow_naive
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models.account import Account
from app.models.category import Category
from app.models.recurring import RecurringTransaction
from app.models.scenario import PlanGoal, Scenario, ScenarioStatus, ScenarioType
from app.models.user import User
from app.rate_limit import get_client_ip, limiter, user_rate_limit_key
from app.rate_limit_overrides import dynamic_limit, load_rate_limit_overrides
from app.schemas.scenario import (
    COMPARE_MAX_SCENARIOS,
    CompareRequest,
    CompareResponse,
    CompareProjection,
    PlanProgressResponse,
    ScenarioCreate,
    ScenarioResponse,
    ScenarioUpdate,
    SimulateRequest,
    validate_horizon,
)
from app.routers.plan_goals import CONFIRM, http_errors
from app.schemas.plan_projection import (
    LifetimeResponse,
    PlanComparisonResponse,
    PlanProjectionResponse,
)
from app.services import plan_draft_service, plan_lifecycle_service, plan_projection_service
from app.services.feature_gate import Feature, require_feature
from app.services.scenario_engine import (
    SimulationRequest,
    build_world_state,
    get_engine,
    simulate_off_loop,
)
from app.services.scenario_engine_ai import run_ai_simulation


logger = logging.getLogger(__name__)

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
    db: AsyncSession, *, user: User, scenario_id: int, write: bool = False
) -> Scenario:
    """Fetch a scenario the current user may see (404 otherwise, NOT 403,
    so a probing client cannot enumerate existence) and, with ``write``,
    change (403: the plan is already visible to them).
    """
    with http_errors():
        return await plan_draft_service.get_plan(
            db, user=user, plan_id=scenario_id, write=write
        )


@router.get("", response_model=list[ScenarioResponse])
async def list_scenarios(
    include_archived: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the scenarios the current user may see: the org's plans and
    their own legacy scenarios.

    By default, archived (is_active=false) rows are hidden. Pass
    ``include_archived=true`` to include them.
    """
    stmt = select(Scenario).where(plan_draft_service.visible_filter(current_user))
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
    """Create a new scenario (Plan in UI terms, a draft) in the current org."""
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
    with http_errors():
        await plan_draft_service.apply_plan_fields(db, row, body.plan_changes())
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


# Declared before ``/{scenario_id}``, which would otherwise take "projection" as an id.
MAX_PROJECTED_PLANS = 20


async def _projection_inputs(
    db: AsyncSession, user: User, plan_ids: list[int], off: list[int], on: list[int]
) -> tuple[list[Scenario], list[PlanGoal]]:
    """The visible plans (404 otherwise) and copies of their goals with the
    toggles applied: a toggle here is for this projection only, never saved."""
    plans, goals = [], []
    for pid in dict.fromkeys(plan_ids):
        plan = await _get_owned_scenario(db, user=user, scenario_id=pid)
        if plan.currency is None:
            raise HTTPException(status_code=422, detail=f"scenario {pid} is not a plan")
        # A plan is in its org's currency scope (refused without one), so the
        # selected plans can never mix currencies.
        with http_errors():
            await plan_draft_service.plan_currency(db, org_id=plan.org_id, requested=plan.currency)
        plans.append(plan)
        for goal in await plan_draft_service.list_goals(db, plan=plan):
            copy = PlanGoal(**{c.key: getattr(goal, c.key)
                               for c in PlanGoal.__mapper__.column_attrs})
            if goal.id in off or goal.id in on:
                copy.enabled = goal.id in on
            goals.append(copy)
    return plans, goals


async def _project(
    db: AsyncSession, plans: list[Scenario], goals: list[PlanGoal], *, lifetime: bool,
    birth_year: Optional[int], return_pct: Optional[Decimal],
    inflation_pct: Optional[Decimal],
) -> PlanProjectionResponse:
    today = date.today()
    org_id = plans[0].org_id
    result = await plan_projection_service.project(
        db, org_id, plans, goals, today=today,
        horizon_end=plan_projection_service.default_horizon_end(today),
    )
    response = PlanProjectionResponse.model_validate(result)
    if lifetime:
        # Assumptions default to the first plan's ("whose timeline").
        first = plans[0]
        response.lifetime = LifetimeResponse.model_validate(await plan_projection_service.lifetime(
            db, org_id, plans, goals, today=today,
            birth_year=birth_year if birth_year is not None else first.birth_year,
            return_pct=return_pct if return_pct is not None else first.return_pct,
            inflation_pct=inflation_pct if inflation_pct is not None else first.inflation_pct,
        ))
    return response


@router.get(
    "/projection",
    response_model=PlanProjectionResponse,
    dependencies=[Depends(load_rate_limit_overrides)],
)
@limiter.limit(dynamic_limit("scenarios.projection", "60/minute"), key_func=user_rate_limit_key)
async def project_plans(
    request: Request,
    plan_id: list[int] = Query(..., min_length=1, max_length=MAX_PROJECTED_PLANS),
    off: list[int] = Query(default=[]),
    on: list[int] = Query(default=[]),
    lifetime: bool = False,
    birth_year: Optional[int] = Query(default=None, ge=1900),
    return_pct: Optional[Decimal] = Query(default=None, ge=-100, le=100),
    inflation_pct: Optional[Decimal] = Query(default=None, ge=-100, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Actual vs With plans for the selected plans together (they affect each
    other), with the per-currency reconciliation, ``off``/``on`` goal toggles
    and, with ``lifetime``, the lifetime tier. Read-only."""
    plans, goals = await _projection_inputs(db, current_user, plan_id, off, on)
    return await _project(db, plans, goals, lifetime=lifetime, birth_year=birth_year,
                          return_pct=return_pct, inflation_pct=inflation_pct)


@router.get(
    "/projection/compare",
    response_model=list[PlanComparisonResponse],
    dependencies=[Depends(load_rate_limit_overrides)],
)
@limiter.limit(
    dynamic_limit("scenarios.projection_compare", "30/minute"), key_func=user_rate_limit_key,
)
async def compare_plan_projections(
    request: Request,
    plan_id: list[int] = Query(..., min_length=1, max_length=MAX_PROJECTED_PLANS),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Each plan projected on its own, side by side: drafts compared. Read-only."""
    plans, goals = await _projection_inputs(db, current_user, plan_id, [], [])
    return [
        PlanComparisonResponse(plan_id=plan.id, projection=await _project(
            db, [plan], [g for g in goals if g.scenario_id == plan.id], lifetime=False,
            birth_year=None, return_pct=None, inflation_pct=None,
        ))
        for plan in plans
    ]


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
    confirm: bool = CONFIRM,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Partial update: name, params, horizon_months, is_active, plan fields.

    scenario_type is immutable post-create (the params blob's
    discriminator pins it). A started plan (TBD-602) takes the fields
    ``plan_draft_service.STARTED_PLAN_FIELDS`` names, with ``confirm=true``,
    under its row lock; a new ``monthly_budget`` reaches its Budgets.
    """
    row = await _get_owned_scenario(
        db, user=current_user, scenario_id=scenario_id, write=True
    )
    with http_errors():
        row = await plan_lifecycle_service.lock(db, row.id)
        plan_draft_service.assert_editable(
            row, confirm=confirm, changes={k: getattr(body, k) for k in body.model_fields_set},
            allowed=plan_draft_service.STARTED_PLAN_FIELDS,
        )

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

    with http_errors():
        await plan_draft_service.apply_plan_fields(db, row, body.plan_changes())
    if row.status == ScenarioStatus.ACTIVE and "monthly_budget" in body.model_fields_set:
        await plan_lifecycle_service.budget_changed(db, plan=row)

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
    flag set via PATCH; this endpoint is the hard remove.) Any state
    (TBD-548): a started plan's series are stopped and its later Budgets
    dropped first; what Start made otherwise stays as ordinary rows (TBD-602).
    """
    row = await _get_owned_scenario(
        db, user=current_user, scenario_id=scenario_id, write=True
    )
    with http_errors():
        await plan_lifecycle_service.delete_plan(db, plan=row)
    await db.commit()


# TBD-602: Start runs the projection to freeze the baseline, and both write
# real rows; limited per user like the engine routes, ``shared_limit`` because
# of ``{scenario_id}``.
@router.post(
    "/{scenario_id}/start",
    response_model=ScenarioResponse,
    dependencies=[Depends(load_rate_limit_overrides)],
)
@limiter.shared_limit(
    dynamic_limit("scenarios.start", "30/minute"), scope="scenarios.start",
    key_func=user_rate_limit_key,
)
async def start_plan(
    scenario_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Draft -> active: the master category, a child per enabled goal, the
    tag, the funding series, the current Budget and the frozen baseline. On an
    active plan, a no-op."""
    row = await _get_owned_scenario(db, user=current_user, scenario_id=scenario_id, write=True)
    with http_errors():
        row = await plan_lifecycle_service.start(db, plan=row)
    await db.commit()
    await db.refresh(row)
    return row


@router.post(
    "/{scenario_id}/finish",
    response_model=ScenarioResponse,
    dependencies=[Depends(load_rate_limit_overrides)],
)
@limiter.shared_limit(
    dynamic_limit("scenarios.finish", "30/minute"), scope="scenarios.finish",
    key_func=user_rate_limit_key,
)
async def finish_plan(
    scenario_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Active -> finished: the series stopped, later Budgets dropped; the
    current Budget and all history stay. On a finished plan, a no-op."""
    row = await _get_owned_scenario(db, user=current_user, scenario_id=scenario_id, write=True)
    with http_errors():
        row = await plan_lifecycle_service.finish(db, plan=row)
    await db.commit()
    await db.refresh(row)
    return row


@router.get("/{scenario_id}/progress", response_model=PlanProgressResponse)
async def plan_progress(
    scenario_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Each goal's progress, keyed on its category and account, and why the
    plan needs attention (empty when healthy). Read-only."""
    row = await _get_owned_scenario(db, user=current_user, scenario_id=scenario_id)
    return PlanProgressResponse(
        plan_id=row.id, status=row.status,
        needs_attention=await plan_lifecycle_service.attention(db, row),
        goals=await plan_lifecycle_service.progress(db, plan=row),
    )


# TBD-726: both engine routes are limited per user, not per address
# (``user_rate_limit_key``). Generous for interactive use: the editor
# re-simulates on a debounced save, about one call a second at most, and a
# compare is an explicit click. ``shared_limit`` because simulate carries
# ``{scenario_id}``: a plain ``limit`` would give every id its own budget.
@router.post(
    "/{scenario_id}/simulate",
    response_model=ScenarioResponse,
    dependencies=[Depends(load_rate_limit_overrides)],
)
@limiter.shared_limit(
    dynamic_limit("scenarios.simulate", "60/minute"),
    scope="scenarios.simulate",
    key_func=user_rate_limit_key,
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
         engine runs in a worker thread (``simulate_off_loop``, TBD-726);
         for ``engine="ai_enhanced"`` we
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
            ip_address=get_client_ip(request),
        )
        engine_name = result.get("engine_name") or "ai_enhanced"
    else:
        # Analytic baseline (default). Sync engine; no DB session
        # passed in — the engine math has no path to the DB. It runs in a
        # worker thread on a detached copy of the row (TBD-726).
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
        result = await simulate_off_loop(
            engine,
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


@router.post(
    "/compare",
    response_model=CompareResponse,
    dependencies=[Depends(load_rate_limit_overrides)],
)
@limiter.limit(
    dynamic_limit("scenarios.compare", "30/minute"),
    key_func=user_rate_limit_key,
)
async def compare_scenarios(
    request: Request,
    body: CompareRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Run the analytic engine on 1-3 scenarios at the SAME horizon
    and return the projections side-by-side.

    Architect-locked rules:

    - Max 3 scenarios (enforced by ``CompareRequest`` Field constraint).
    - Each scenario must be visible to the current user; 404 otherwise
      (matches the rest of the router).
    - The request's horizon is validated against EACH scenario's
      ``scenario_type`` cap. If any scenario rejects it, the whole
      compare 422s with the offending scenario id in the message.
    - Engine runs are sequential — sub-second each, max 3, so parallel
      gather adds complexity for no measurable win. Each runs in a worker
      thread, off the event loop (TBD-726).
    - World state is built ONCE and reused across all engine runs (it
      depends only on org_id/user_id, not on the scenario).
    """
    # Load every scenario the request asked for, in the SAME order as
    # the request's scenario_ids list. Order preserved so the response
    # is positionally parallel.
    rows: list[Scenario] = []
    for sid in body.scenario_ids:
        row = await db.get(Scenario, sid)
        if row is None or not plan_draft_service.can_view(current_user, row):
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
        result = await simulate_off_loop(
            engine,
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
