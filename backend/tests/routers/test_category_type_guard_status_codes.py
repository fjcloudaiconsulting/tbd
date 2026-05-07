"""Router-level tests pinning the HTTP status code surface for the
category/type compatibility guard. Service-level invariants are covered
by tests/services/test_transaction_service_category_type_guard.py and
test_forecast_plan_category_type_guard.py.

ValidationError → 400 (matches existing project convention; see
app/main.py validation_handler).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user
from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.billing import BillingPeriod
from app.models.category import CategoryType
from app.models.forecast_plan import ForecastPlan, PlanStatus
from app.models.user import Role, User
from app.routers.forecast_plans import router as forecast_plans_router
from app.routers.transactions import router as transactions_router
from app.security import hash_password
from app.services.exceptions import ConflictError, NotFoundError, ValidationError


@pytest_asyncio.fixture
async def session_factory():
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
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        yield factory
    finally:
        await engine.dispose()


def make_app(session_factory) -> FastAPI:
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user() -> User:
        from sqlalchemy import select as _select
        async with session_factory() as db:
            return (
                await db.execute(
                    _select(User).where(User.is_superadmin.is_(True))
                )
            ).scalar_one()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user

    @app.exception_handler(NotFoundError)
    async def _nf(_req, exc: NotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    async def _ve(_req, exc: ValidationError):
        return JSONResponse(status_code=400, content={"detail": exc.detail})

    @app.exception_handler(ConflictError)
    async def _ce(_req, exc: ConflictError):
        return JSONResponse(status_code=409, content={"detail": exc.detail})

    app.include_router(transactions_router)
    app.include_router(forecast_plans_router)
    return app


async def _seed(factory) -> dict:
    """Org + superadmin user + accounts + master categories + period + draft plan."""
    async with factory() as db:
        org = Organization(name="Test Org", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id,
            username="root",
            email="root@example.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            is_superadmin=True,
            is_active=True,
            email_verified=True,
        )
        at = AccountType(
            org_id=org.id, name="Checking", slug="checking", is_system=True
        )
        db.add_all([user, at])
        await db.flush()
        acct = Account(
            org_id=org.id, name="Acct", account_type_id=at.id,
            balance=Decimal("1000"), currency="EUR",
        )
        db.add(acct)
        await db.flush()
        expense_master = Category(
            org_id=org.id, name="Groceries", slug="groceries",
            type=CategoryType.EXPENSE,
        )
        income_master = Category(
            org_id=org.id, name="Salary", slug="salary",
            type=CategoryType.INCOME,
        )
        db.add_all([expense_master, income_master])
        await db.flush()
        period = BillingPeriod(
            org_id=org.id, start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 31),
        )
        db.add(period)
        await db.flush()
        plan = ForecastPlan(
            org_id=org.id, billing_period_id=period.id,
            status=PlanStatus.DRAFT,
        )
        db.add(plan)
        await db.commit()
        return {
            "org_id": org.id,
            "acct_id": acct.id,
            "expense_master_id": expense_master.id,
            "income_master_id": income_master.id,
            "plan_id": plan.id,
        }


@pytest.mark.asyncio
async def test_post_transaction_with_mismatched_category_returns_400(
    session_factory,
):
    seed = await _seed(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/transactions",
            json={
                "account_id": seed["acct_id"],
                "category_id": seed["income_master_id"],
                "description": "rent",
                "amount": "500",
                "type": "expense",
                "status": "settled",
                "date": "2026-05-01",
            },
        )
    assert resp.status_code == 400
    assert "category" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_forecast_plan_upsert_with_mismatched_category_returns_400(
    session_factory,
):
    seed = await _seed(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/forecast-plans/{seed['plan_id']}/items",
            json={
                "category_id": seed["expense_master_id"],
                "type": "income",
                "planned_amount": "100",
                "source": "manual",
            },
        )
    assert resp.status_code == 400
    assert "category" in resp.json()["detail"].lower()
