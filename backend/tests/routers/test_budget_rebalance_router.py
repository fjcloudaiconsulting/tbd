"""TBD-461 — the router half: G-B9 (product gate + transfer route gone)."""
from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.main import app as full_app
from app.models import Base
from app.models.billing import BillingPeriod
from app.models.budget import Budget
from app.models.category import Category, CategoryType
from app.models.settings import OrgSetting
from app.models.user import Organization, Role, User
from app.routers.budgets import router as budgets_router
from app.security import hash_password
from app.services.feature_gate import Feature, org_preference_key


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
        org = Organization(name="Rebal Org", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        admin = User(
            org_id=org.id,
            username="rebal-admin",
            email="rebal-admin@example.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.ADMIN,
            is_superadmin=False,
            is_active=True,
            email_verified=True,
        )
        db.add(admin)
        await db.flush()
        period = BillingPeriod(org_id=org.id, start_date=__import__("datetime").date.today(), end_date=None)
        db.add(period)
        cat_a = Category(org_id=org.id, name="A", type=CategoryType.EXPENSE)
        cat_b = Category(org_id=org.id, name="B", type=CategoryType.EXPENSE)
        db.add_all([cat_a, cat_b])
        await db.flush()
        budget_a = Budget(
            org_id=org.id, category_id=cat_a.id, amount=Decimal("100.00"),
            period_start=period.start_date, period_end=None,
        )
        budget_b = Budget(
            org_id=org.id, category_id=cat_b.id, amount=Decimal("50.00"),
            period_start=period.start_date, period_end=None,
        )
        db.add_all([budget_a, budget_b])
        await db.commit()
        return {
            "org_id": org.id, "admin_id": admin.id,
            "budget_a": budget_a.id, "budget_b": budget_b.id,
        }


def _make_app(factory, user_id: int) -> FastAPI:
    app = FastAPI()

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with factory() as s:
            yield s

    async def override_user() -> User:
        async with factory() as db:
            from sqlalchemy import select
            return (await db.execute(select(User).where(User.id == user_id))).scalar_one()

    def override_factory():
        return factory

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_session_factory] = override_factory
    app.dependency_overrides[get_current_user] = override_user
    app.include_router(budgets_router)
    return app


@pytest.mark.asyncio
async def test_gb9_transfer_route_absent_from_the_app(session_factory):
    """/api/v1/budgets/transfer is gone from app.routes."""
    paths = {getattr(r, "path", None) for r in full_app.routes}
    assert "/api/v1/budgets/transfer" not in paths


@pytest.mark.asyncio
async def test_gb9_rebalance_404s_with_budgets_off(session_factory):
    ids = await _seed(session_factory)
    async with session_factory() as db:
        db.add(
            OrgSetting(
                org_id=ids["org_id"],
                key=org_preference_key(Feature.BUDGETS),
                value="off",
            )
        )
        await db.commit()

    app = _make_app(session_factory, ids["admin_id"])
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/budgets/rebalance",
            json={
                "items": [
                    {"budget_id": ids["budget_a"], "expected_amount": "100.00", "amount": "110.00"},
                    {"budget_id": ids["budget_b"], "expected_amount": "50.00", "amount": "40.00"},
                ]
            },
        )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_rebalance_endpoint_happy_path(session_factory):
    ids = await _seed(session_factory)
    app = _make_app(session_factory, ids["admin_id"])
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/budgets/rebalance",
            json={
                "items": [
                    {"budget_id": ids["budget_a"], "expected_amount": "100.00", "amount": "110.00"},
                    {"budget_id": ids["budget_b"], "expected_amount": "50.00", "amount": "40.00"},
                ]
            },
        )
    assert res.status_code == 200
    by_id = {row["id"]: row for row in res.json()}
    assert by_id[ids["budget_a"]]["amount"] == "110.00"
    assert by_id[ids["budget_b"]]["amount"] == "40.00"
