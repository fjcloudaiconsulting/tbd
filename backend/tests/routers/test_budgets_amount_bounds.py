"""BE-01 at the wire: budget amounts with unbounded exponents are a 422.

Before the fix ``BudgetCreate.amount`` was ``Decimal = Field(gt=0)``. The
value reached the ORM row unchanged and PyMySQL rendered it as a literal with
one character per unit of exponent. The request must now fail validation
before the handler, and nothing may be written.
"""
from __future__ import annotations

import datetime
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.billing import BillingPeriod
from app.models.budget import Budget
from app.models.category import Category, CategoryType
from app.models.user import Organization, Role, User
from app.routers.budgets import router as budgets_router
from app.security import hash_password
from tests.factories import make_test_app


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


async def _seed(factory) -> dict:
    async with factory() as db:
        org = Organization(name="Money Org", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id, username="money-member", email="money-member@example.com",
            password_hash=hash_password("pw-1234567"), role=Role.MEMBER,
            is_active=True, email_verified=True,
        )
        db.add(user)
        db.add(BillingPeriod(org_id=org.id, start_date=datetime.date.today(), end_date=None))
        cat = Category(org_id=org.id, name="Food", type=CategoryType.EXPENSE)
        db.add(cat)
        await db.commit()
        return {"user": user, "category_id": cat.id}


async def _budget_count(factory) -> int:
    async with factory() as db:
        return await db.scalar(select(func.count()).select_from(Budget))


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", ["1E+100000", "1e999999", "1E-100000", "NaN", "12.345"])
async def test_create_budget_rejects_unbounded_amount_with_422(session_factory, amount):
    seed = await _seed(session_factory)
    app = make_test_app(session_factory, routers=budgets_router, current_user=seed["user"])
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/budgets", json={"category_id": seed["category_id"], "amount": amount}
        )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", "amount"]
    assert await _budget_count(session_factory) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", ["125.50", 125.5, "9999999999.99"])
async def test_create_and_update_budget_still_accept_ordinary_amounts(session_factory, amount):
    seed = await _seed(session_factory)
    app = make_test_app(session_factory, routers=budgets_router, current_user=seed["user"])
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/budgets", json={"category_id": seed["category_id"], "amount": amount}
        )
        assert created.status_code == 201, created.text
        assert Decimal(created.json()["amount"]) == Decimal(str(amount))
        updated = client.put(f"/api/v1/budgets/{created.json()['id']}", json={"amount": "80.00"})
        assert updated.status_code == 200, updated.text
        assert Decimal(updated.json()["amount"]) == Decimal("80.00")


@pytest.mark.asyncio
async def test_update_budget_rejects_unbounded_amount_with_422(session_factory):
    seed = await _seed(session_factory)
    app = make_test_app(session_factory, routers=budgets_router, current_user=seed["user"])
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/budgets", json={"category_id": seed["category_id"], "amount": "10.00"}
        )
        assert created.status_code == 201, created.text
        resp = client.put(f"/api/v1/budgets/{created.json()['id']}", json={"amount": "1E+100000"})
    assert resp.status_code == 422, resp.text
    async with session_factory() as db:
        assert (await db.scalar(select(Budget.amount))) == Decimal("10.00")
