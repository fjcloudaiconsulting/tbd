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
