"""Pydantic schemas for the Plans simulation sandbox (spec 2026-05-22).

Architect-locked rules baked into this module:

- Discriminated union by ``scenario_type`` on ``params``. Trip +
  Purchase params are fully specified in PR1; Retirement + Custom
  ship minimal stubs (full schema lands in PR2).
- Horizon caps split by ``scenario_type``: 120 months for
  trip/purchase/custom, 480 months for retirement. The
  ``validate_horizon`` helper is the single source of truth and
  is called from both the create / patch path and the simulate
  request path.
- Internal name = ``scenarios``. The user-facing label is "Plans";
  schemas use the internal name throughout.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.scenario import ScenarioType


# Horizon caps per scenario type. Architect-locked 2026-05-22:
# 120 months (10y) for trip / purchase / custom (interventions
# the user might fold back into reality within a decade); 480
# months (40y) for retirement (a working-life projection).
HORIZON_CAP_BY_TYPE: dict[str, int] = {
    "trip": 120,
    "purchase": 120,
    "custom": 120,
    "retirement": 480,
}

# Minimum horizon is 1 month; zero would project nothing.
HORIZON_MIN_MONTHS = 1

NAME_MIN_LENGTH = 1
NAME_MAX_LENGTH = 120


def validate_horizon(scenario_type: str, horizon_months: int) -> None:
    """Raise ValueError when ``horizon_months`` exceeds the per-type cap.

    Shared between create / patch / simulate paths so the cap lives
    in exactly one place. Pre-launch architect lock: the column
    allows up to 480 at storage; the cap is enforced at the request
    validator on EVERY mutating path, never at the column.
    """
    if horizon_months < HORIZON_MIN_MONTHS:
        raise ValueError(
            f"horizon_months must be >= {HORIZON_MIN_MONTHS}"
        )
    cap = HORIZON_CAP_BY_TYPE.get(scenario_type)
    if cap is None:
        raise ValueError(f"unknown scenario_type: {scenario_type}")
    if horizon_months > cap:
        raise ValueError(
            f"horizon_months exceeds cap for {scenario_type} ({cap})"
        )


# ── Plan-type params ────────────────────────────────────────────────────


class _TripExtra(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=120)
    amount: Decimal
    on_date: date


class TripParams(BaseModel):
    """Trip scenario params (spec §Plan templates, ``trip``).

    Engine derivation: lump-sum expense on ``start_date`` for
    ``transport_cost + accommodation_per_night * duration_days +
    daily_budget * duration_days + sum(extras)`` against
    ``source_account_id``.
    """

    model_config = ConfigDict(extra="forbid")

    scenario_type: Literal["trip"]
    destination: str = Field(min_length=1, max_length=200)
    start_date: date
    duration_days: int = Field(ge=1, le=365)
    currency: str = Field(min_length=3, max_length=3)
    transport_cost: Decimal = Field(ge=0)
    accommodation_per_night: Decimal = Field(ge=0)
    daily_budget: Decimal = Field(ge=0)
    one_off_extras: list[_TripExtra] = Field(default_factory=list)
    source_account_id: int


class _PurchaseFinancing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal: Decimal = Field(ge=0)
    annual_rate_pct: Decimal = Field(ge=0)
    term_months: int = Field(ge=1, le=480)
    first_payment_date: date
    payment_account_id: int


class PurchaseParams(BaseModel):
    """Purchase scenario params (car / house / big-ticket items).

    Engine derivation: lump-sum expense (``down_payment``) on
    ``target_date`` against ``down_payment_account_id``. Then, if
    ``financing`` is set, a monthly amortized expense from
    ``first_payment_date`` for ``term_months`` months. If
    ``financing`` is None, the full ``total_price`` is the lump.
    """

    model_config = ConfigDict(extra="forbid")

    scenario_type: Literal["purchase"]
    subtype: str = Field(min_length=1, max_length=60)
    label: str = Field(min_length=1, max_length=200)
    target_date: date
    currency: str = Field(min_length=3, max_length=3)
    total_price: Decimal = Field(ge=0)
    down_payment: Decimal = Field(ge=0)
    down_payment_account_id: int
    financing: Optional[_PurchaseFinancing] = None


class _RetirementCurveStep(BaseModel):
    """One step in the ``contribution_curve``.

    ``from_date`` is the first month the step applies (the field name is
    serialized as ``from`` in JSON to match the spec's wire shape; ``from``
    is a Python reserved word so the Python field uses ``from_date`` and is
    aliased on serialization). For any month ``m >= from_date``, the
    monthly contribution becomes ``monthly`` and overrides the base
    ``monthly_contribution``. Steps are evaluated in ascending date order;
    a later step takes precedence over an earlier one.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_date: date = Field(alias="from")
    monthly: Decimal = Field(ge=0)


class RetirementParams(BaseModel):
    """Retirement scenario params (spec §Plan templates, ``retirement``).

    Engine derivation: monthly contribution lands in ``contribution_account_id``,
    compounded at ``annual_return_pct / 12`` per month. The optional
    ``contribution_curve`` is a step function ascending in date; for any
    month at or after a step's ``from``, the contribution overrides
    ``monthly_contribution``. ``inflation_pct`` is the assumed annual
    inflation rate used to deflate the projected balance into real terms
    (the chart overlays a "real-terms" line alongside the nominal one).

    The architect-locked verdict bands for retirement compare the
    real-terms balance at ``target_retirement_date`` to ``target_balance``:
    green if real >= target, yellow if within 15% below, red otherwise.
    """

    model_config = ConfigDict(extra="forbid")

    scenario_type: Literal["retirement"]
    target_retirement_date: date
    currency: str = Field(min_length=3, max_length=3)
    monthly_contribution: Decimal = Field(ge=0)
    contribution_account_id: int
    target_balance: Decimal = Field(ge=0)
    annual_return_pct: Decimal = Field(ge=0, le=100)
    inflation_pct: Decimal = Field(default=Decimal("2.5"), ge=0, le=100)
    contribution_curve: list[_RetirementCurveStep] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_curve(self):
        # Steps must be monotonic ascending by ``from`` date; same-day
        # entries are rejected (they're never meaningful and almost
        # always a UI bug). The curve editor surfaces the same error
        # client-side, but we re-validate server-side because UI
        # validation is hygiene, not a contract.
        prev = None
        for step in self.contribution_curve:
            if prev is not None and step.from_date <= prev:
                raise ValueError(
                    "contribution_curve must be strictly ascending by 'from' date"
                )
            prev = step.from_date
        return self


# ── Custom event primitives (PR3 of the Plans train) ────────────────────
#
# The ``custom`` plan_type's ``events`` array is a discriminated union of
# five primitives, all keyed on ``type``. All months are RELATIVE to the
# scenario start (month 0 = start month). Cross-user FK references
# (recurring_id / account_id / category_id) are validated against the
# current user's org at the router boundary because the schema layer
# has no DB session.


class _CustomEventIncomeOff(BaseModel):
    """Zero out income recurring patterns for a month range."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["income_off"]
    from_month: int = Field(ge=0)
    to_month: Optional[int] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _check_range(self):
        if self.to_month is not None and self.to_month < self.from_month:
            raise ValueError(
                "from_month must be <= to_month"
            )
        return self


class _CustomEventExpenseOff(BaseModel):
    """Zero out expense recurring patterns for a month range.

    Optional ``category_ids`` scopes the silencing to specific
    categories; omitted means every expense recurring is silenced.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["expense_off"]
    from_month: int = Field(ge=0)
    to_month: Optional[int] = Field(default=None, ge=0)
    category_ids: Optional[list[int]] = None

    @model_validator(mode="after")
    def _check_range(self):
        if self.to_month is not None and self.to_month < self.from_month:
            raise ValueError(
                "from_month must be <= to_month"
            )
        return self


class _CustomEventRecurringOn(BaseModel):
    """Explicitly INCLUDE a specific recurring for the given range.

    PR3 punt: PR1's engine already includes ALL active recurring
    by default, so this event is a no-op until a future
    ``exclude_recurring`` base flag exists. The schema accepts it
    so the UI can hand-author the event today.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["recurring_on"]
    recurring_id: int
    from_month: int = Field(ge=0)
    to_month: Optional[int] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _check_range(self):
        if self.to_month is not None and self.to_month < self.from_month:
            raise ValueError(
                "from_month must be <= to_month"
            )
        return self


class _CustomEventOneOffIncome(BaseModel):
    """Single-month income injection into ``account_id``."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["one_off_income"]
    month: int = Field(ge=0)
    amount: Decimal = Field(ge=0)
    account_id: int
    category_id: Optional[int] = None


class _CustomEventOneOffExpense(BaseModel):
    """Single-month expense charge against ``account_id``."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["one_off_expense"]
    month: int = Field(ge=0)
    amount: Decimal = Field(ge=0)
    account_id: int
    category_id: Optional[int] = None


CustomEvent = Annotated[
    Union[
        _CustomEventIncomeOff,
        _CustomEventExpenseOff,
        _CustomEventRecurringOn,
        _CustomEventOneOffIncome,
        _CustomEventOneOffExpense,
    ],
    Field(discriminator="type"),
]


class CustomParams(BaseModel):
    """Custom scenario params (PR3 of the Plans train).

    ``events`` is a discriminated-union list of the five primitive
    event types. Each event's months are RELATIVE to the scenario
    start (month 0 = start month). Range validation (``from_month``
    <= ``to_month``) lives on each event's model_validator. The
    router additionally validates each event's month/from_month/to_month
    against the scenario's horizon and resolves cross-user FK leaks
    on recurring_id / account_id / category_id.
    """

    model_config = ConfigDict(extra="forbid")

    scenario_type: Literal["custom"]
    label: str = Field(min_length=1, max_length=200)
    events: list[CustomEvent] = Field(default_factory=list)


ScenarioParams = Annotated[
    Union[TripParams, PurchaseParams, RetirementParams, CustomParams],
    Field(discriminator="scenario_type"),
]


# ── Top-level Scenario shapes ───────────────────────────────────────────


class ScenarioCreate(BaseModel):
    """Create payload for ``POST /api/v1/scenarios``.

    The discriminated union pins ``params`` to exactly the shape that
    matches ``scenario_type``. Mismatching the two returns a 422 from
    Pydantic before the router ever runs.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=NAME_MIN_LENGTH, max_length=NAME_MAX_LENGTH)
    scenario_type: ScenarioType
    params: ScenarioParams
    horizon_months: int = Field(default=24, ge=HORIZON_MIN_MONTHS)

    @model_validator(mode="after")
    def _check_type_matches_params_and_horizon(self):
        # Discriminator enforces type<->params shape match, but the
        # outer scenario_type field is the source of truth on the row.
        # Pin them so a future schema tweak can't drift them apart.
        if self.params.scenario_type != self.scenario_type.value:
            raise ValueError(
                "scenario_type and params.scenario_type must match"
            )
        validate_horizon(self.scenario_type.value, self.horizon_months)
        return self


class ScenarioUpdate(BaseModel):
    """Partial update payload for ``PATCH /api/v1/scenarios/{id}``.

    Allowed mutations: name, params, horizon_months, is_active. The
    scenario_type is immutable post-create (changing it would invalidate
    the params blob's shape).
    """

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(
        default=None, min_length=NAME_MIN_LENGTH, max_length=NAME_MAX_LENGTH
    )
    params: Optional[ScenarioParams] = None
    horizon_months: Optional[int] = Field(
        default=None, ge=HORIZON_MIN_MONTHS
    )
    is_active: Optional[bool] = None


class ScenarioResponse(BaseModel):
    """Read shape returned by every scenarios endpoint."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    user_id: int
    name: str
    scenario_type: ScenarioType
    params_json: dict[str, Any]
    projection_json: Optional[dict[str, Any]] = None
    projection_engine: Optional[str] = None
    projection_computed_at: Optional[datetime] = None
    horizon_months: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


class SimulateRequest(BaseModel):
    """Body for ``POST /api/v1/scenarios/{id}/simulate``.

    The horizon override on the request is OPTIONAL — when None, the
    engine uses the row's stored ``horizon_months``. When supplied, it
    is validated against the cap for the row's scenario_type by the
    router before the engine runs.

    ``smooth_with_regression`` (PR2 of the Plans train) tells the engine
    to fit a least-squares regression on the last 12 months of the user's
    per-account net cashflow and overlay the projected drift on top of
    the deterministic projection. Default False keeps PR1's exact math.
    Applies to all plan_types.
    """

    model_config = ConfigDict(extra="forbid")

    engine: Literal["analytic", "ai_enhanced"] = "analytic"
    options: dict[str, Any] = Field(default_factory=dict)
    horizon_months: Optional[int] = Field(default=None, ge=HORIZON_MIN_MONTHS)
    smooth_with_regression: bool = False


# ── Projection (engine output) shape ────────────────────────────────────
#
# Mirrors the spec's "Output shape" section. Engine returns Decimals
# converted to strings so JSON transport stays lossless.


class ProjectionPoint(BaseModel):
    month: str
    projected_balance: str


class AccountSeries(BaseModel):
    account_id: int
    account_name: str
    currency: str
    points: List[ProjectionPoint]


class DipAlert(BaseModel):
    account_id: int
    month: str
    projected_balance: str
    trigger: str
    severity: Literal["info", "warn", "critical"] = "warn"


class AffordabilityVerdict(BaseModel):
    color: Literal["green", "yellow", "red"]
    headline: str
    reason: str


class Suggestion(BaseModel):
    action: str
    expected_outcome: str
    # Numeric hint (e.g. days to shift, amount to reduce) is optional
    # and free-form so suggestions can evolve without schema churn.
    by_days: Optional[int] = None
    by_amount: Optional[str] = None


class RealTermsSeries(BaseModel):
    """Inflation-adjusted "real terms" balance series (retirement only).

    Mirrors ``ProjectionPoint`` shape but reports the projected balance
    in today's purchasing power (deflated by ``inflation_pct``). Surfaced
    in the chart as an overlay line alongside the nominal projection.
    """

    points: List[ProjectionPoint]
    inflation_pct: str


class ProjectionResult(BaseModel):
    engine_name: str
    computed_at: datetime
    horizon_months: int
    currency: str
    per_account_series: List[AccountSeries]
    alerts: List[DipAlert]
    verdict: AffordabilityVerdict
    suggestions: List[Suggestion]
    # Retirement-only. Empty for non-retirement plans.
    real_terms_series: Optional[RealTermsSeries] = None
    # Set True when the regression overlay was applied (PR2). Helps the
    # UI label the chart and helps tests assert the path was taken.
    smoothed_with_regression: bool = False


# ── PR3 of the Plans train: comparison view ─────────────────────────────


# Architect-locked: max 3 scenarios side-by-side. 4+ would make the
# chart visually unreadable; the cap is enforced here so the router
# returns 422 (not 200 with truncation) on overflow.
COMPARE_MIN_SCENARIOS = 1
COMPARE_MAX_SCENARIOS = 3


class CompareRequest(BaseModel):
    """Body for ``POST /api/v1/scenarios/compare`` (PR3).

    Runs the analytic engine on each scenario at the SAME horizon so
    the projections can be overlaid on one chart. The horizon is
    validated against every scenario's per-type cap before the engine
    runs; if any scenario rejects it, the WHOLE compare fails with
    422 and the offending scenario id in the detail.
    """

    model_config = ConfigDict(extra="forbid")

    scenario_ids: List[int] = Field(
        min_length=COMPARE_MIN_SCENARIOS,
        max_length=COMPARE_MAX_SCENARIOS,
    )
    horizon_months: int = Field(ge=HORIZON_MIN_MONTHS)
    smooth_with_regression: bool = False


class CompareProjection(BaseModel):
    """One scenario's projection enriched with its name + type so the
    UI can label series without a second round-trip.
    """

    scenario_id: int
    name: str
    scenario_type: ScenarioType
    projection: ProjectionResult


class CompareResponse(BaseModel):
    """Response for ``POST /api/v1/scenarios/compare``.

    Order is parallel to the request's ``scenario_ids`` so the
    frontend can index by position.
    """

    projections: List[CompareProjection]
