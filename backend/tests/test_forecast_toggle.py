"""TBD-197 PR 2 — org-level Forecast toggle.

Fence ids match specs/2026-08-04-planning-tool-toggles.md §8 "PR 2 — backend".

The rule PR 1 established and this file keeps (spec §8): **every fence names
the row it writes.** No test here says "forecast off" — each one writes an
explicit ``OrgSetting(org, "orgpref.forecast", "off")`` row and says so.

F7 is the load-bearing one. ``/api/v1/forecast/account-balances`` is an
ACCOUNT-projection engine (credit-card statement cycles + loan amortization)
that merely lives under a ``/forecast`` URL prefix; it reads no ``ForecastPlan``
and no ``Budget``, and ``LoanPayoffTile`` / ``CreditUtilizationWidget`` consume
it. Moving the Forecast gate from the ``GET /api/v1/forecast`` handler up to
``forecast.py``'s router would close it and break Loans and Credit Cards
silently — nothing else in the suite notices. F7 is the fence that notices.
"""
from __future__ import annotations

import datetime
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user, get_current_user_optional, get_session_factory
from app.models import Base
from app.models.billing import BillingPeriod
from app.models.category import Category, CategoryType
from app.models.forecast_plan import (
    ForecastItemType,
    ForecastPlan,
    ForecastPlanItem,
    ItemSource,
    PlanStatus,
)
from app.models.settings import OrgSetting
from app.models.user import Organization, Role, User
from app.routers.budgets import router as budgets_router
from app.routers.forecast import router as forecast_router
from app.routers.forecast_plans import router as forecast_plans_router
from app.security import hash_password
from app.services.feature_gate import Feature, org_preference_key


# ── fixtures ─────────────────────────────────────────────────────────────────


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


async def _seed(factory: async_sessionmaker[AsyncSession]) -> dict:
    async with factory() as db:
        org = Organization(name="Forecast Org", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        admin = User(
            org_id=org.id,
            username="forecast-admin",
            email="forecast-admin@example.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.ADMIN,
            is_superadmin=False,
            is_active=True,
            email_verified=True,
        )
        db.add(admin)
        await db.commit()
        return {"org_id": org.id, "admin_id": admin.id}


async def _seed_period_and_plan(factory, org_id: int) -> None:
    """Seed an OPEN billing period plus a forecast plan with one expense item.

    ⚠ F8's control needs this. ``create_budgets_from_forecast`` raises
    ``ValidationError`` when the resolved period has no ``ForecastPlan``, and
    in a bare ``FastAPI()`` test app (no exception handlers registered) that
    PROPAGATES out of the TestClient rather than becoming the production 400 —
    so an unseeded control fails for a reason that has nothing to do with the
    gate under test.
    """
    async with factory() as db:
        period = BillingPeriod(
            org_id=org_id,
            start_date=datetime.date(2026, 5, 1),
            end_date=None,
        )
        category = Category(
            org_id=org_id, name="Groceries", type=CategoryType.EXPENSE
        )
        db.add_all([period, category])
        await db.flush()
        plan = ForecastPlan(
            org_id=org_id,
            billing_period_id=period.id,
            status=PlanStatus.ACTIVE,
        )
        db.add(plan)
        await db.flush()
        db.add(
            ForecastPlanItem(
                plan_id=plan.id,
                org_id=org_id,
                category_id=category.id,
                type=ForecastItemType.EXPENSE,
                planned_amount=Decimal("100.00"),
                source=ItemSource.MANUAL,
            )
        )
        await db.commit()


async def _get_user(factory, user_id: int) -> User:
    async with factory() as db:
        return (
            await db.execute(select(User).where(User.id == user_id))
        ).scalar_one()


def _make_app(factory, routers, user_id: int) -> FastAPI:
    app = FastAPI()

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with factory() as s:
            yield s

    async def override_user() -> User:
        return await _get_user(factory, user_id)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_session_factory] = lambda: factory
    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_current_user_optional] = override_user
    for r in routers:
        app.include_router(r)
    return app


async def _write_org_row(factory, org_id: int, key: str, value: str) -> None:
    async with factory() as db:
        db.add(OrgSetting(org_id=org_id, key=key, value=value))
        await db.commit()


# ── F7 — THE fence: account-balances survives a Forecast opt-out ─────────────


@pytest.mark.asyncio
async def test_f7_account_balances_stays_open_while_forecast_closes(session_factory):
    """F7. Writes ``OrgSetting(org, "orgpref.forecast", "off")``.

    Observes BOTH routes on ``forecast.py``:

      ``GET /api/v1/forecast/account-balances`` → **200**
      ``GET /api/v1/forecast``                  → **404**

    Mutant killed: the Forecast dep moved off the ``GET /api/v1/forecast``
    handler and onto ``forecast.py``'s ``APIRouter(...)`` — the "fix the
    inconsistency" edit the next engineer is invited to make. Under it,
    account-balances 404s, ``LoanPayoffTile`` and ``CreditUtilizationWidget``
    lose their data source, and no other test in the suite goes red.
    """
    ids = await _seed(session_factory)
    await _write_org_row(
        session_factory, ids["org_id"], org_preference_key(Feature.FORECAST), "off"
    )
    app = _make_app(session_factory, [forecast_router], ids["admin_id"])
    with TestClient(app) as client:
        balances = client.get("/api/v1/forecast/account-balances")
        projection = client.get("/api/v1/forecast")

    assert balances.status_code == 200, balances.text
    # Not merely "not 404": a real payload, so a route that answered 200 with
    # an error envelope could not pass.
    assert "accounts" in balances.json()
    assert projection.status_code == 404, projection.text


@pytest.mark.asyncio
async def test_f7_control_no_orgpref_row_keeps_both_open(session_factory):
    """F7 control. **No** ``orgpref.forecast`` row → both routes 200.

    Without this a gate that 404s ``GET /api/v1/forecast`` unconditionally
    would pass F7.
    """
    ids = await _seed(session_factory)
    app = _make_app(session_factory, [forecast_router], ids["admin_id"])
    with TestClient(app) as client:
        balances = client.get("/api/v1/forecast/account-balances")
        projection = client.get("/api/v1/forecast")

    assert balances.status_code == 200, balances.text
    assert projection.status_code == 200, projection.text


# ── F7b — the forecast-plans router closes wholesale ─────────────────────────


@pytest.mark.asyncio
async def test_f7b_orgpref_off_closes_the_forecast_plans_router(session_factory):
    """F7b. Writes ``OrgSetting(org, "orgpref.forecast", "off")``.

    Observes THREE differently-shaped handlers on that router — ``GET
    /api/v1/forecast-plans``, ``GET .../current`` and ``POST .../populate`` —
    all **404**. Three, not one, because the fence has to pin the
    ROUTER-level dep: a dep applied per-handler that reached only the one
    endpoint a single-request fence happened to name would otherwise leave
    eleven handlers open.
    """
    ids = await _seed(session_factory)
    await _write_org_row(
        session_factory, ids["org_id"], org_preference_key(Feature.FORECAST), "off"
    )
    app = _make_app(session_factory, [forecast_plans_router], ids["admin_id"])
    with TestClient(app) as client:
        plan = client.get("/api/v1/forecast-plans")
        current = client.get("/api/v1/forecast-plans/current")
        populate = client.post("/api/v1/forecast-plans/populate")

    assert plan.status_code == 404, plan.text
    assert current.status_code == 404, current.text
    assert populate.status_code == 404, populate.text


@pytest.mark.asyncio
async def test_f7b_control_no_orgpref_row_keeps_forecast_plans_open(session_factory):
    """F7b control. **No** ``orgpref.forecast`` row → the same three answer 200."""
    ids = await _seed(session_factory)
    app = _make_app(session_factory, [forecast_plans_router], ids["admin_id"])
    with TestClient(app) as client:
        plan = client.get("/api/v1/forecast-plans")
        current = client.get("/api/v1/forecast-plans/current")
        populate = client.post("/api/v1/forecast-plans/populate")

    assert plan.status_code == 200, plan.text
    assert current.status_code == 200, current.text
    assert populate.status_code == 200, populate.text


# ── F8 — the cross-feature gate on POST /budgets/from-forecast ───────────────


@pytest.mark.asyncio
async def test_f8_from_forecast_closes_while_budgets_stays_open(session_factory):
    """F8. Writes ``OrgSetting(org, "orgpref.forecast", "off")`` — and NOTHING
    in the ``budgets`` namespace.

    Observes ``POST /api/v1/budgets/from-forecast`` → **404** while
    ``GET /api/v1/budgets`` → **200**.

    Mutant killed: the cross-feature handler-level dep omitted. That endpoint
    reads a ``ForecastPlan``; an org that switched Forecast off must not reach
    it, and the router-level ``BUDGETS`` gate does not close it because Budgets
    is still on.
    """
    ids = await _seed(session_factory)
    await _seed_period_and_plan(session_factory, ids["org_id"])
    await _write_org_row(
        session_factory, ids["org_id"], org_preference_key(Feature.FORECAST), "off"
    )
    app = _make_app(session_factory, [budgets_router], ids["admin_id"])
    with TestClient(app) as client:
        seeded = client.post("/api/v1/budgets/from-forecast")
        listed = client.get("/api/v1/budgets")

    assert seeded.status_code == 404, seeded.text
    assert listed.status_code == 200, listed.text


@pytest.mark.asyncio
async def test_f8_control_forecast_on_seeds_budgets(session_factory):
    """F8 control. **No** ``orgpref.forecast`` row, with a seeded open period
    and a ``ForecastPlan`` carrying one expense item → **200**.

    The seed is mandatory: without a plan the service raises ``ValidationError``,
    which a bare ``FastAPI()`` app propagates rather than converting to the
    production 400 — the control would fail for a reason unrelated to the gate.
    """
    ids = await _seed(session_factory)
    await _seed_period_and_plan(session_factory, ids["org_id"])
    app = _make_app(session_factory, [budgets_router], ids["admin_id"])
    with TestClient(app) as client:
        res = client.post("/api/v1/budgets/from-forecast")
    assert res.status_code == 200, res.text
    assert len(res.json()) == 1


@pytest.mark.asyncio
async def test_f8b_budgets_opt_out_also_closes_from_forecast(session_factory):
    """F8b. Writes ``OrgSetting(org, "orgpref.budgets", "off")`` and NOTHING in
    the ``forecast`` namespace.

    ``POST /api/v1/budgets/from-forecast`` → **404**, from the PR-1 router-level
    ``BUDGETS`` gate. Pins that the new handler-level ``FORECAST`` dep is an
    ADDITIONAL gate, not a replacement: a build that moved the Budgets gate
    onto the handlers and dropped it from the router would pass F8 and leak
    seven other handlers.
    """
    ids = await _seed(session_factory)
    await _seed_period_and_plan(session_factory, ids["org_id"])
    await _write_org_row(
        session_factory, ids["org_id"], org_preference_key(Feature.BUDGETS), "off"
    )
    app = _make_app(session_factory, [budgets_router], ids["admin_id"])
    with TestClient(app) as client:
        seeded = client.post("/api/v1/budgets/from-forecast")
        listed = client.get("/api/v1/budgets")

    assert seeded.status_code == 404, seeded.text
    assert listed.status_code == 404, listed.text
