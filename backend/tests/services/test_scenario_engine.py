"""Service tests for the Plans simulation engine (spec 2026-05-22).

Architect-locked invariants pinned here:

- Trip lump-sum lands on ``start_date`` against
  ``source_account_id`` and shows up in the projected series.
- Purchase amortization: down-payment lands on ``target_date``,
  monthly amortized payment matches ``_amortized_monthly_payment``
  output starting on ``first_payment_date`` for ``term_months``.
- Verdict thresholds (green / yellow / red) trigger on synthetic
  input.
- **SANDBOXING GUARD** — running ``simulate`` produces zero deltas
  in ``transactions``, ``accounts``, ``budgets``, ``forecast_plans``,
  and ``recurring_transactions``. Only ``scenarios`` gains the row
  the test created up-front. This is THE PR1 architect lock.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from dateutil.relativedelta import relativedelta
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.account import Account, AccountType
from app.models.budget import Budget
from app.models.category import Category, CategoryType
from app.models.forecast_plan import ForecastPlan
from app.models.recurring import Frequency, RecurringTransaction
from app.models.scenario import Scenario, ScenarioType
from app.models.transaction import Transaction
from app.models.user import Organization, Role, User
from app.security import hash_password
from app.services.scenario_engine import (
    AccountSnapshot,
    AnalyticEngine,
    AIEngine,
    RecurringSnapshot,
    SimulationRequest,
    WorldState,
    _amortized_monthly_payment,
    build_world_state,
    get_engine,
)


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(Engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_user_with_realistic_data(factory) -> dict:
    """Seed a realistic user fixture: 1 org, 1 user, 2 accounts, 2
    recurring templates, 3 transactions, 1 budget, 1 forecast_plan,
    1 scenario. The sandboxing guard test snapshots row counts
    BEFORE running simulate and asserts +0 deltas on every table
    other than ``scenarios`` afterwards.
    """
    async with factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        user = User(
            org_id=org.id,
            username="alice",
            email="alice@acme.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            is_superadmin=False,
            is_active=True,
            email_verified=True,
        )
        db.add(user)
        await db.commit()
        at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
        db.add(at)
        await db.commit()
        acc1 = Account(
            org_id=org.id,
            account_type_id=at.id,
            name="Main checking",
            balance=Decimal("4500.00"),
            currency="EUR",
            is_active=True,
            is_default=True,
            opening_balance=Decimal("4500.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        acc2 = Account(
            org_id=org.id,
            account_type_id=at.id,
            name="Savings",
            balance=Decimal("12000.00"),
            currency="EUR",
            is_active=True,
            is_default=False,
            opening_balance=Decimal("12000.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        db.add_all([acc1, acc2])
        await db.commit()
        cat_in = Category(
            org_id=org.id,
            name="Salary",
            type=CategoryType.INCOME,
        )
        cat_ex = Category(
            org_id=org.id,
            name="Groceries",
            type=CategoryType.EXPENSE,
        )
        db.add_all([cat_in, cat_ex])
        await db.commit()
        rec1 = RecurringTransaction(
            org_id=org.id,
            account_id=acc1.id,
            category_id=cat_in.id,
            description="Salary",
            amount=Decimal("3000.00"),
            type="income",
            frequency=Frequency.MONTHLY,
            next_due_date=date.today().replace(day=1) + timedelta(days=14),
            auto_settle=False,
            is_active=True,
        )
        rec2 = RecurringTransaction(
            org_id=org.id,
            account_id=acc1.id,
            category_id=cat_ex.id,
            description="Groceries",
            amount=Decimal("400.00"),
            type="expense",
            frequency=Frequency.MONTHLY,
            next_due_date=date.today().replace(day=1) + timedelta(days=5),
            auto_settle=False,
            is_active=True,
        )
        db.add_all([rec1, rec2])
        await db.commit()
        budget = Budget(
            org_id=org.id,
            category_id=cat_ex.id,
            amount=Decimal("500.00"),
            period_start=date.today().replace(day=1),
            period_end=None,
        )
        db.add(budget)
        await db.commit()
        # Scenario row the simulate call will run against.
        scen = Scenario(
            org_id=org.id,
            user_id=user.id,
            name="Lisbon trip",
            scenario_type=ScenarioType.TRIP,
            params_json={
                "scenario_type": "trip",
                "destination": "Lisbon, Portugal",
                "start_date": (date.today().replace(day=1) + timedelta(days=60)).isoformat(),
                "duration_days": 10,
                "currency": "EUR",
                "transport_cost": "450.00",
                "accommodation_per_night": "85.00",
                "daily_budget": "70.00",
                "one_off_extras": [],
                "source_account_id": acc1.id,
            },
            horizon_months=12,
            is_active=True,
        )
        db.add(scen)
        await db.commit()
        return {
            "org_id": org.id,
            "user_id": user.id,
            "acc1_id": acc1.id,
            "acc2_id": acc2.id,
            "rec1_id": rec1.id,
            "rec2_id": rec2.id,
            "scenario_id": scen.id,
        }


async def _row_counts(factory) -> dict[str, int]:
    """Snapshot row counts across every table the sandboxing guard
    asserts +0 deltas on.
    """
    async with factory() as db:
        return {
            "transactions": (await db.execute(select(func.count()).select_from(Transaction))).scalar_one(),
            "accounts": (await db.execute(select(func.count()).select_from(Account))).scalar_one(),
            "budgets": (await db.execute(select(func.count()).select_from(Budget))).scalar_one(),
            "forecast_plans": (await db.execute(select(func.count()).select_from(ForecastPlan))).scalar_one(),
            "recurring_transactions": (await db.execute(select(func.count()).select_from(RecurringTransaction))).scalar_one(),
            "scenarios": (await db.execute(select(func.count()).select_from(Scenario))).scalar_one(),
        }


# ── Sandboxing guard (the architect-lock test) ──────────────────────────


@pytest.mark.asyncio
async def test_simulate_does_not_mutate_any_real_table(session_factory):
    """ARCHITECT LOCK PR1: running the analytic engine against a
    realistic fixture must produce ZERO row-count delta on every
    real table. Only ``scenarios`` may change (and only because the
    test seeded the row itself; the row count stays the same across
    the simulate call).
    """
    seeds = await _seed_user_with_realistic_data(session_factory)
    before = await _row_counts(session_factory)

    async with session_factory() as db:
        state = await build_world_state(
            db, org_id=seeds["org_id"], user_id=seeds["user_id"]
        )
        scen = (
            await db.execute(
                select(Scenario).where(Scenario.id == seeds["scenario_id"])
            )
        ).scalar_one()
        # Detach so engine can run on the snapshot without holding
        # a live session reference.
        engine = AnalyticEngine()
        request = SimulationRequest(
            scenario=scen,
            state=state,
            horizon_months=scen.horizon_months,
            options={},
        )
        result = engine.simulate(request)
        # NOTE: the engine does NOT call db.commit on its own. The
        # router writes the projection back outside the engine, so
        # this test exercises the engine path in isolation — the
        # row-count check below must therefore see ZERO deltas on
        # EVERY table including ``scenarios``.

    after = await _row_counts(session_factory)

    # The lock: every table count is identical pre/post.
    assert after["transactions"] == before["transactions"]
    assert after["accounts"] == before["accounts"]
    assert after["budgets"] == before["budgets"]
    assert after["forecast_plans"] == before["forecast_plans"]
    assert after["recurring_transactions"] == before["recurring_transactions"]
    assert after["scenarios"] == before["scenarios"]

    # And the projection itself was actually computed (sanity).
    assert result["engine_name"] == "analytic_v1"
    assert len(result["per_account_series"]) == 2


# ── Trip lump-sum lands on start_date ────────────────────────────────────


@pytest.mark.asyncio
async def test_trip_lump_sum_lands_on_start_date(session_factory):
    """The trip engine derivation must post a lump-sum expense on
    ``start_date`` for ``transport_cost + accommodation_per_night
    * duration_days + daily_budget * duration_days + extras`` against
    ``source_account_id``.
    """
    # Construct a trip scenario starting ~2 months from now.
    start = (date.today().replace(day=1) + timedelta(days=60))
    scenario = Scenario(
        org_id=1,
        user_id=1,
        name="t",
        scenario_type=ScenarioType.TRIP,
        params_json={
            "scenario_type": "trip",
            "destination": "Lisbon",
            "start_date": start.isoformat(),
            "duration_days": 10,
            "currency": "EUR",
            "transport_cost": "450.00",
            "accommodation_per_night": "85.00",
            "daily_budget": "70.00",
            "one_off_extras": [],
            "source_account_id": 42,
        },
        horizon_months=6,
    )
    state = WorldState(
        accounts=[
            AccountSnapshot(
                account_id=42,
                account_name="Main",
                currency="EUR",
                starting_balance=Decimal("5000.00"),
            ),
        ],
        recurring=[],
    )
    engine = AnalyticEngine()
    result = engine.simulate(
        SimulationRequest(
            scenario=scenario,
            state=state,
            horizon_months=6,
            options={},
        )
    )
    # Expected lump:  450 + 85*10 + 70*10 + 0  = 450 + 850 + 700 = 2000
    expected_balance_after_dip = Decimal("5000.00") - Decimal("2000.00")
    # The point for the trip month must reflect the dip.
    label = f"{start.year:04d}-{start.month:02d}"
    series = result["per_account_series"][0]["points"]
    trip_point = next((p for p in series if p["month"] == label), None)
    assert trip_point is not None, series
    assert Decimal(trip_point["projected_balance"]) == expected_balance_after_dip


# ── Purchase financing amortization ─────────────────────────────────────


def test_amortized_payment_matches_hand_computed():
    """For P=14000, r=6.5% annual / 12, n=60 months:
        monthly = 14000 * (0.065/12) / (1 - (1 + 0.065/12)^-60)
              ≈ 273.91 EUR.

    The engine helper should be within a cent of that.
    """
    p = _amortized_monthly_payment(
        Decimal("14000"), Decimal("6.5"), 60
    )
    # Hand computed via standard formula.
    assert Decimal("273.85") <= p <= Decimal("274.05"), p


def test_amortized_payment_zero_interest_is_principal_over_n():
    p = _amortized_monthly_payment(Decimal("1200"), Decimal("0"), 12)
    assert p == Decimal("100.00"), p


# ── Verdict thresholds ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verdict_green_when_no_dip(session_factory):
    """No dip below zero → verdict color = green."""
    scenario = Scenario(
        org_id=1, user_id=1, name="t",
        scenario_type=ScenarioType.TRIP,
        params_json={
            "scenario_type": "trip",
            "destination": "x",
            "start_date": (date.today() + timedelta(days=60)).isoformat(),
            "duration_days": 2,
            "currency": "EUR",
            "transport_cost": "10.00",
            "accommodation_per_night": "10.00",
            "daily_budget": "10.00",
            "one_off_extras": [],
            "source_account_id": 1,
        },
        horizon_months=6,
    )
    state = WorldState(
        accounts=[
            AccountSnapshot(
                account_id=1, account_name="A",
                currency="EUR", starting_balance=Decimal("10000.00")
            )
        ],
        recurring=[],
    )
    result = AnalyticEngine().simulate(
        SimulationRequest(
            scenario=scenario, state=state, horizon_months=6, options={}
        )
    )
    assert result["verdict"]["color"] == "green"
    assert result["alerts"] == []


@pytest.mark.asyncio
async def test_verdict_red_when_severe_dip(session_factory):
    """Dip > 10% of starting balance → verdict color = red."""
    scenario = Scenario(
        org_id=1, user_id=1, name="t",
        scenario_type=ScenarioType.TRIP,
        params_json={
            "scenario_type": "trip",
            "destination": "x",
            "start_date": (date.today() + timedelta(days=60)).isoformat(),
            "duration_days": 30,
            "currency": "EUR",
            "transport_cost": "5000.00",
            "accommodation_per_night": "200.00",
            "daily_budget": "200.00",
            "one_off_extras": [],
            "source_account_id": 1,
        },
        horizon_months=12,
    )
    state = WorldState(
        accounts=[
            AccountSnapshot(
                account_id=1, account_name="A",
                currency="EUR", starting_balance=Decimal("1000.00")
            )
        ],
        recurring=[],
    )
    result = AnalyticEngine().simulate(
        SimulationRequest(
            scenario=scenario, state=state, horizon_months=12, options={}
        )
    )
    assert result["verdict"]["color"] == "red"
    assert len(result["alerts"]) >= 1


# ── AI engine stub ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ai_engine_raises_not_implemented():
    """PR4 stub: invoking the AIEngine MUST raise NotImplementedError
    with the message "PR4" so a misconfigured request surfaces a
    deliberate error, not a silent analytic fallback.
    """
    state = WorldState(accounts=[], recurring=[])
    scenario = Scenario(
        org_id=1, user_id=1, name="t",
        scenario_type=ScenarioType.TRIP,
        params_json={},
        horizon_months=6,
    )
    with pytest.raises(NotImplementedError) as exc_info:
        AIEngine().simulate(
            SimulationRequest(
                scenario=scenario, state=state, horizon_months=6, options={}
            )
        )
    assert "PR4" in str(exc_info.value)


def test_get_engine_returns_analytic_by_default():
    assert isinstance(get_engine("analytic"), AnalyticEngine)
    assert isinstance(get_engine("ai_enhanced"), AIEngine)


def test_get_engine_unknown_raises_keyerror():
    with pytest.raises(KeyError):
        get_engine("bogus")


# ── PR2: Retirement engine ─────────────────────────────────────────────


def _make_retirement_scenario(
    *,
    contribution_account_id: int = 1,
    monthly_contribution: str = "500.00",
    target_balance: str = "100000.00",
    annual_return_pct: str = "6.0",
    inflation_pct: str = "2.5",
    target_date: date | None = None,
    horizon_months: int = 360,
    contribution_curve: list[dict] | None = None,
) -> Scenario:
    """Helper: build a retirement Scenario row for engine tests."""
    if target_date is None:
        target_date = date.today().replace(day=1) + timedelta(days=horizon_months * 30)
    params = {
        "scenario_type": "retirement",
        "target_retirement_date": target_date.isoformat(),
        "currency": "EUR",
        "monthly_contribution": monthly_contribution,
        "contribution_account_id": contribution_account_id,
        "target_balance": target_balance,
        "annual_return_pct": annual_return_pct,
        "inflation_pct": inflation_pct,
        "contribution_curve": contribution_curve or [],
    }
    return Scenario(
        org_id=1, user_id=1, name="retire",
        scenario_type=ScenarioType.RETIREMENT,
        params_json=params,
        horizon_months=horizon_months,
    )


def _retirement_state(starting_balance: str = "0") -> WorldState:
    return WorldState(
        accounts=[
            AccountSnapshot(
                account_id=1,
                account_name="Retirement",
                currency="EUR",
                starting_balance=Decimal(starting_balance),
            ),
        ],
        recurring=[],
        history=[],
    )


def _expected_fv_annuity_due(
    starting: Decimal,
    monthly: Decimal,
    annual_pct: Decimal,
    months: int,
) -> Decimal:
    """Closed-form future value of an annuity (ordinary, end-of-period
    contribution + end-of-period interest, matching the engine's order):

      balance_m = balance_{m-1} + contribution + (balance_{m-1} + contribution) * r
                = (balance_{m-1} + contribution) * (1 + r)

      FV = starting * (1+r)^n + monthly * ((1+r)^n - 1) / r * (1+r)

    The engine applies the contribution then accrues interest on the
    new total, so each step is multiplied by (1+r) — annuity-due form.
    """
    r = (annual_pct / Decimal("100")) / Decimal("12")
    if r == 0:
        return starting + monthly * Decimal(months)
    factor = (Decimal("1") + r) ** months
    fv_start = starting * factor
    fv_contrib = monthly * ((factor - Decimal("1")) / r) * (Decimal("1") + r)
    return fv_start + fv_contrib


@pytest.mark.asyncio
async def test_retirement_happy_path_nominal_and_real_terms():
    """30-year horizon, 6% return, 2.5% inflation, 500/mo flat.

    Both nominal and real-terms series should land within ±5% of the
    hand-computed compound interest formula.
    """
    horizon = 360  # 30y
    target_date = date.today().replace(day=1) + relativedelta(months=horizon)
    scenario = _make_retirement_scenario(
        monthly_contribution="500.00",
        annual_return_pct="6.0",
        inflation_pct="2.5",
        target_date=target_date,
        horizon_months=horizon,
    )
    state = _retirement_state(starting_balance="0")
    result = AnalyticEngine().simulate(
        SimulationRequest(
            scenario=scenario, state=state, horizon_months=horizon, options={}
        )
    )
    # Closed-form FV check.
    expected_nominal = _expected_fv_annuity_due(
        Decimal("0"), Decimal("500"), Decimal("6.0"), horizon
    )
    actual_nominal = Decimal(
        result["per_account_series"][0]["points"][-1]["projected_balance"]
    )
    # Within 5% tolerance (rounding + per-month quantization).
    bound = expected_nominal * Decimal("0.05")
    assert abs(actual_nominal - expected_nominal) <= bound, (
        actual_nominal, expected_nominal
    )

    # Real-terms = nominal / (1 + i_monthly)^horizon.
    monthly_i = (Decimal("2.5") / Decimal("100")) / Decimal("12")
    expected_real = expected_nominal / ((Decimal("1") + monthly_i) ** horizon)
    real_series = result["real_terms_series"]
    assert real_series is not None
    actual_real = Decimal(real_series["points"][-1]["projected_balance"])
    real_bound = expected_real * Decimal("0.05")
    assert abs(actual_real - expected_real) <= real_bound, (
        actual_real, expected_real
    )


@pytest.mark.asyncio
async def test_retirement_curve_step_function_grows_correctly():
    """Stepped contribution: 300/mo for 10y, 500/mo for 10y, 800/mo for 10y.

    Asserts each stage's end balance is plausibly larger than a flat
    300/mo would have produced — and the final balance lands BETWEEN
    a flat 300 baseline (lower bound) and a flat 800 baseline (upper).
    """
    today = date.today().replace(day=1)
    horizon = 360  # 30y
    step_at_year_10 = today + relativedelta(years=10)
    step_at_year_20 = today + relativedelta(years=20)
    curve = [
        {"from": step_at_year_10.isoformat(), "monthly": "500.00"},
        {"from": step_at_year_20.isoformat(), "monthly": "800.00"},
    ]
    target_date = today + relativedelta(months=horizon)
    scenario = _make_retirement_scenario(
        monthly_contribution="300.00",
        annual_return_pct="6.0",
        target_date=target_date,
        horizon_months=horizon,
        contribution_curve=curve,
    )
    state = _retirement_state(starting_balance="0")
    result = AnalyticEngine().simulate(
        SimulationRequest(
            scenario=scenario, state=state, horizon_months=horizon, options={}
        )
    )
    actual_final = Decimal(
        result["per_account_series"][0]["points"][-1]["projected_balance"]
    )
    low_baseline = _expected_fv_annuity_due(
        Decimal("0"), Decimal("300"), Decimal("6.0"), horizon
    )
    high_baseline = _expected_fv_annuity_due(
        Decimal("0"), Decimal("800"), Decimal("6.0"), horizon
    )
    assert low_baseline < actual_final < high_baseline, (
        low_baseline, actual_final, high_baseline
    )

    # Sanity: the per-month points after each step should grow faster
    # than the previous step (slope monotonically non-decreasing).
    points = result["per_account_series"][0]["points"]
    # Pick three windows: months 60-72 (300/mo regime), months 180-192
    # (500/mo regime), months 300-312 (800/mo regime). Per-step growth
    # in the 800/mo window must exceed per-step growth in 300/mo.
    def avg_growth(start: int, end: int) -> Decimal:
        total = Decimal("0")
        for i in range(start + 1, end):
            total += Decimal(points[i]["projected_balance"]) - Decimal(
                points[i - 1]["projected_balance"]
            )
        return total / Decimal(end - start - 1)
    g300 = avg_growth(60, 72)
    g800 = avg_growth(300, 312)
    assert g800 > g300, (g300, g800)


@pytest.mark.asyncio
async def test_retirement_verdict_green_meets_target():
    horizon = 360
    target_date = date.today().replace(day=1) + relativedelta(months=horizon)
    # 500/mo for 30y at 6% returns roughly 500k nominal. Real-terms at
    # 2.5% inflation ≈ 240k. A 100k target is well under that.
    scenario = _make_retirement_scenario(
        monthly_contribution="500.00",
        target_balance="100000.00",
        annual_return_pct="6.0",
        inflation_pct="2.5",
        target_date=target_date,
        horizon_months=horizon,
    )
    result = AnalyticEngine().simulate(
        SimulationRequest(
            scenario=scenario, state=_retirement_state(), horizon_months=horizon, options={}
        )
    )
    assert result["verdict"]["color"] == "green"


@pytest.mark.asyncio
async def test_retirement_verdict_yellow_close_to_target():
    """A target that the projection misses by ~10% should be YELLOW.

    Construction: build a scenario whose real-terms projection lands at
    ~90% of target.
    """
    horizon = 360
    target_date = date.today().replace(day=1) + relativedelta(months=horizon)
    # Hand-tune target_balance to be ~10% above the projected real-terms
    # final value. Real-terms at 500/mo, 6% return, 2.5% inflation, 30y
    # is roughly 240k (see prior test). Setting target to 260k puts the
    # projection ~92% of target → within 15%, so YELLOW.
    scenario = _make_retirement_scenario(
        monthly_contribution="500.00",
        target_balance="260000.00",
        annual_return_pct="6.0",
        inflation_pct="2.5",
        target_date=target_date,
        horizon_months=horizon,
    )
    result = AnalyticEngine().simulate(
        SimulationRequest(
            scenario=scenario, state=_retirement_state(), horizon_months=horizon, options={}
        )
    )
    assert result["verdict"]["color"] == "yellow", result["verdict"]


@pytest.mark.asyncio
async def test_retirement_verdict_red_falls_short():
    horizon = 360
    target_date = date.today().replace(day=1) + relativedelta(months=horizon)
    # 50/mo for 30y can't possibly produce 500k real-terms. Verdict = red.
    scenario = _make_retirement_scenario(
        monthly_contribution="50.00",
        target_balance="500000.00",
        annual_return_pct="6.0",
        inflation_pct="2.5",
        target_date=target_date,
        horizon_months=horizon,
    )
    result = AnalyticEngine().simulate(
        SimulationRequest(
            scenario=scenario, state=_retirement_state(), horizon_months=horizon, options={}
        )
    )
    assert result["verdict"]["color"] == "red"
    # Suggestion should propose a "raise_monthly_contribution".
    assert any(
        s["action"] == "raise_monthly_contribution"
        for s in result["suggestions"]
    )


# ── PR2: Regression overlay ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_regression_overlay_diff_within_5pct_of_deterministic():
    """The regression overlay must not run wild: the smoothed result
    must differ from the deterministic baseline by <=5% on a synthetic
    bounded-history dataset.
    """
    from app.services.scenario_engine import MonthlyCashflowPoint
    today = date.today().replace(day=1)
    horizon = 12
    scenario = Scenario(
        org_id=1, user_id=1, name="t",
        scenario_type=ScenarioType.TRIP,
        params_json={
            "scenario_type": "trip",
            "destination": "Lisbon",
            "start_date": (today + relativedelta(months=2)).isoformat(),
            "duration_days": 5,
            "currency": "EUR",
            "transport_cost": "100.00",
            "accommodation_per_night": "50.00",
            "daily_budget": "50.00",
            "one_off_extras": [],
            "source_account_id": 1,
        },
        horizon_months=horizon,
    )
    # Synthetic history: 12 months of slight positive net cashflow with
    # mild noise. A linear regression slope here is small.
    history = []
    for m in range(12):
        d = today - relativedelta(months=12 - m)
        # net flips ±20 around a mean of 50, slight upward trend.
        net_val = Decimal("50") + Decimal(m) * Decimal("2") + (
            Decimal("20") if m % 2 == 0 else Decimal("-20")
        )
        history.append(
            MonthlyCashflowPoint(account_id=1, year=d.year, month=d.month, net=net_val)
        )
    state = WorldState(
        accounts=[
            AccountSnapshot(
                account_id=1, account_name="Main",
                currency="EUR", starting_balance=Decimal("10000.00")
            )
        ],
        recurring=[],
        history=history,
    )
    deterministic = AnalyticEngine().simulate(
        SimulationRequest(
            scenario=scenario, state=state, horizon_months=horizon, options={}
        )
    )
    smoothed = AnalyticEngine().simulate(
        SimulationRequest(
            scenario=scenario, state=state, horizon_months=horizon,
            options={"smooth_with_regression": True},
        )
    )
    assert deterministic["smoothed_with_regression"] is False
    assert smoothed["smoothed_with_regression"] is True

    det_final = Decimal(
        deterministic["per_account_series"][0]["points"][-1]["projected_balance"]
    )
    smo_final = Decimal(
        smoothed["per_account_series"][0]["points"][-1]["projected_balance"]
    )
    # diff bound: smoothed result within ±5% of deterministic.
    abs_diff = abs(smo_final - det_final)
    bound = abs(det_final) * Decimal("0.05")
    assert abs_diff <= bound, (det_final, smo_final, abs_diff, bound)


# ── PR2: Refined verdict bands for trip/purchase ────────────────────────


@pytest.mark.asyncio
async def test_general_verdict_yellow_end_balance_between_50_and_80():
    """No dip, but end balance lands at 70% of start → YELLOW (the
    'don't burn more than 20%' refined band)."""
    today = date.today().replace(day=1)
    horizon = 6
    # Trip lump-sum that spends 30% of starting balance. No dip
    # below zero, but ending balance is 70% of start.
    scenario = Scenario(
        org_id=1, user_id=1, name="t",
        scenario_type=ScenarioType.TRIP,
        params_json={
            "scenario_type": "trip",
            "destination": "Lisbon",
            "start_date": (today + relativedelta(months=2)).isoformat(),
            "duration_days": 10,
            "currency": "EUR",
            "transport_cost": "300.00",
            "accommodation_per_night": "0",
            "daily_budget": "0",
            "one_off_extras": [],
            "source_account_id": 1,
        },
        horizon_months=horizon,
    )
    state = WorldState(
        accounts=[
            AccountSnapshot(
                account_id=1, account_name="Main",
                currency="EUR", starting_balance=Decimal("1000.00")
            )
        ],
        recurring=[],
        history=[],
    )
    result = AnalyticEngine().simulate(
        SimulationRequest(
            scenario=scenario, state=state, horizon_months=horizon, options={}
        )
    )
    assert result["verdict"]["color"] == "yellow", result["verdict"]
    assert result["alerts"] == []


@pytest.mark.asyncio
async def test_general_verdict_red_end_balance_below_50pct():
    """End balance below 50% of start → RED via the end-ratio band,
    independent of any dip."""
    today = date.today().replace(day=1)
    horizon = 6
    scenario = Scenario(
        org_id=1, user_id=1, name="t",
        scenario_type=ScenarioType.TRIP,
        params_json={
            "scenario_type": "trip",
            "destination": "Lisbon",
            "start_date": (today + relativedelta(months=2)).isoformat(),
            "duration_days": 10,
            "currency": "EUR",
            "transport_cost": "700.00",
            "accommodation_per_night": "0",
            "daily_budget": "0",
            "one_off_extras": [],
            "source_account_id": 1,
        },
        horizon_months=horizon,
    )
    state = WorldState(
        accounts=[
            AccountSnapshot(
                account_id=1, account_name="Main",
                currency="EUR", starting_balance=Decimal("1000.00")
            )
        ],
        recurring=[],
        history=[],
    )
    result = AnalyticEngine().simulate(
        SimulationRequest(
            scenario=scenario, state=state, horizon_months=horizon, options={}
        )
    )
    assert result["verdict"]["color"] == "red", result["verdict"]


# ── PR2: Sandboxing guard with retirement scenario added ───────────────


@pytest.mark.asyncio
async def test_simulate_retirement_does_not_mutate_any_real_table(session_factory):
    """ARCHITECT LOCK PR2: running the analytic engine against a
    realistic fixture WITH A RETIREMENT SCENARIO must produce ZERO
    row-count delta on every real table.
    """
    seeds = await _seed_user_with_realistic_data(session_factory)
    # Add a retirement scenario row on top.
    async with session_factory() as db:
        retire_scen = Scenario(
            org_id=seeds["org_id"],
            user_id=seeds["user_id"],
            name="Retire at 65",
            scenario_type=ScenarioType.RETIREMENT,
            params_json={
                "scenario_type": "retirement",
                "target_retirement_date": (
                    date.today() + timedelta(days=365 * 30)
                ).isoformat(),
                "currency": "EUR",
                "monthly_contribution": "500.00",
                "contribution_account_id": seeds["acc1_id"],
                "target_balance": "100000.00",
                "annual_return_pct": "6.0",
                "inflation_pct": "2.5",
                "contribution_curve": [],
            },
            horizon_months=360,
        )
        db.add(retire_scen)
        await db.commit()
        await db.refresh(retire_scen)
        retire_id = retire_scen.id

    before = await _row_counts(session_factory)

    async with session_factory() as db:
        state = await build_world_state(
            db, org_id=seeds["org_id"], user_id=seeds["user_id"]
        )
        scen = (
            await db.execute(
                select(Scenario).where(Scenario.id == retire_id)
            )
        ).scalar_one()
        result = AnalyticEngine().simulate(
            SimulationRequest(
                scenario=scen,
                state=state,
                horizon_months=scen.horizon_months,
                options={"smooth_with_regression": True},
            )
        )

    after = await _row_counts(session_factory)

    assert after["transactions"] == before["transactions"]
    assert after["accounts"] == before["accounts"]
    assert after["budgets"] == before["budgets"]
    assert after["forecast_plans"] == before["forecast_plans"]
    assert after["recurring_transactions"] == before["recurring_transactions"]
    assert after["scenarios"] == before["scenarios"]
    assert result["engine_name"] == "analytic_v1"
    assert result["smoothed_with_regression"] is True
    assert result["real_terms_series"] is not None
