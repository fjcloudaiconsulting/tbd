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


class RetirementParams(BaseModel):
    """Retirement scenario params — minimal stub for PR1.

    Full retirement UX (contribution curve editor, projected balance
    visualization) lands in PR2. PR1 accepts the shape so a user can
    create a retirement plan and stash params; ``simulate`` runs the
    analytic engine on the existing fields.
    """

    model_config = ConfigDict(extra="forbid")

    scenario_type: Literal["retirement"]
    target_retirement_date: date
    currency: str = Field(min_length=3, max_length=3)
    monthly_contribution: Decimal = Field(ge=0)
    contribution_account_id: int
    target_balance: Decimal = Field(ge=0)
    annual_return_pct: Decimal = Field(ge=0, le=100)


class CustomParams(BaseModel):
    """Custom scenario params — minimal stub for PR1.

    The full ``events`` event-primitives editor is PR2 territory.
    PR1 ships the shape (label + opaque event list) so users can
    sketch a custom scenario today; simulate ignores the events
    (no engine support yet — also PR2).
    """

    model_config = ConfigDict(extra="forbid")

    scenario_type: Literal["custom"]
    label: str = Field(min_length=1, max_length=200)
    events: list[dict[str, Any]] = Field(default_factory=list)


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
    """

    model_config = ConfigDict(extra="forbid")

    engine: Literal["analytic", "ai_enhanced"] = "analytic"
    options: dict[str, Any] = Field(default_factory=dict)
    horizon_months: Optional[int] = Field(default=None, ge=HORIZON_MIN_MONTHS)


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


class ProjectionResult(BaseModel):
    engine_name: str
    computed_at: datetime
    horizon_months: int
    currency: str
    per_account_series: List[AccountSeries]
    alerts: List[DipAlert]
    verdict: AffordabilityVerdict
    suggestions: List[Suggestion]
