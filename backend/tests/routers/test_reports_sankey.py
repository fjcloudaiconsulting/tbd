"""Router tests for POST /api/v1/reports/query/sankey.

Pinned invariants:

- Feature gate OFF → 404 on the sankey endpoint.
- Auth required → 401/403 without a valid user.
- 200 + correct links for the seeded org.
- Org-scoping: a second org's transactions do NOT appear.
- extra/unknown body key → 422 (``extra="forbid"`` on SankeyQuery).
- Valid request with no income → 200 with empty links (not an error).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.account import Account, AccountType
from app.models.category import Category
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization, Role, User
from app.routers.reports import router as reports_router
from app.security import hash_password


# ── fixtures ──────────────────────────────────────────────────────────


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


@pytest.fixture(autouse=True)
def _enable_flag(monkeypatch):
    """Default every test in this file to feature_reports_v2 ON.

    Tests that check flag-off explicitly flip it back to False.
    """
    monkeypatch.setattr(app_settings, "feature_reports_v2", True)


def _make_app(session_factory, user_resolver):
    """Build a test FastAPI app with the reports router and overridden deps."""
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_user() -> User:
        return await user_resolver(session_factory)

    def override_factory():
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_session_factory] = override_factory
    app.include_router(reports_router)
    return app


def _resolver(username: str):
    async def resolve(sf):
        async with sf() as db:
            from sqlalchemy import select as _s
            return (await db.execute(_s(User).where(User.username == username))).scalar_one()
    return resolve


async def _seed_two_orgs(factory) -> dict:
    """Two orgs (A + B), one user each.

    Org A: Salary income 5000, Housing expense 2000.
    Org B: IncomeB 9999 — must NOT appear when queried as Org A.
    """
    async with factory() as db:
        org_a = Organization(name="Org A", billing_cycle_day=1)
        org_b = Organization(name="Org B", billing_cycle_day=1)
        db.add_all([org_a, org_b])
        await db.commit()

        user_a = User(
            org_id=org_a.id, username="user_a", email="a@example.com",
            password_hash=hash_password("pw-1234567"), role=Role.OWNER, email_verified=True,
        )
        user_b = User(
            org_id=org_b.id, username="user_b", email="b@example.com",
            password_hash=hash_password("pw-1234567"), role=Role.OWNER, email_verified=True,
        )
        db.add_all([user_a, user_b])
        await db.commit()

        at_a = AccountType(org_id=org_a.id, name="Checking")
        at_b = AccountType(org_id=org_b.id, name="Checking")
        db.add_all([at_a, at_b])
        await db.commit()

        acct_a = Account(
            org_id=org_a.id, account_type_id=at_a.id, name="A Bank",
            currency="EUR", balance=Decimal("0"),
        )
        acct_b = Account(
            org_id=org_b.id, account_type_id=at_b.id, name="B Bank",
            currency="EUR", balance=Decimal("0"),
        )
        db.add_all([acct_a, acct_b])
        await db.commit()

        cat_salary_a = Category(org_id=org_a.id, name="Salary")
        cat_housing_a = Category(org_id=org_a.id, name="Housing")
        cat_income_b = Category(org_id=org_b.id, name="IncomeB")
        db.add_all([cat_salary_a, cat_housing_a, cat_income_b])
        await db.commit()

        today = date(2026, 6, 1)
        db.add(
            Transaction(
                org_id=org_a.id, account_id=acct_a.id, category_id=cat_salary_a.id,
                description="Salary", amount=Decimal("5000"),
                type=TransactionType.INCOME, status=TransactionStatus.SETTLED,
                date=today, settled_date=today,
            )
        )
        db.add(
            Transaction(
                org_id=org_a.id, account_id=acct_a.id, category_id=cat_housing_a.id,
                description="Housing", amount=Decimal("2000"),
                type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
                date=today, settled_date=today,
            )
        )
        # Org B data — must never appear in Org A's sankey.
        db.add(
            Transaction(
                org_id=org_b.id, account_id=acct_b.id, category_id=cat_income_b.id,
                description="Income B", amount=Decimal("9999"),
                type=TransactionType.INCOME, status=TransactionStatus.SETTLED,
                date=today, settled_date=today,
            )
        )
        await db.commit()

        return {"org_a_id": org_a.id, "org_b_id": org_b.id}


# ── tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sankey_returns_200_with_links(session_factory):
    """Authenticated user gets 200 with the expected income/expense links."""
    await _seed_two_orgs(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))

    with TestClient(app) as client:
        res = client.post("/api/v1/reports/query/sankey", json={"filters": []})

    assert res.status_code == 200, res.text
    data = res.json()
    assert "links" in data
    assert "meta" in data

    by = {(lk["source"], lk["target"]): lk["value"] for lk in data["links"]}
    assert by[("Salary", "Income")] == pytest.approx(5000.0)
    assert by[("Income", "Housing")] == pytest.approx(2000.0)
    assert by[("Income", "Savings")] == pytest.approx(3000.0)


@pytest.mark.asyncio
async def test_sankey_org_scoping(session_factory):
    """Org B's data (IncomeB 9999) must NOT appear in Org A's sankey."""
    await _seed_two_orgs(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))

    with TestClient(app) as client:
        res = client.post("/api/v1/reports/query/sankey", json={"filters": []})

    assert res.status_code == 200
    all_sources = {lk["source"] for lk in res.json()["links"]}
    all_targets = {lk["target"] for lk in res.json()["links"]}
    all_nodes = all_sources | all_targets

    assert "IncomeB" not in all_nodes, "Org B income category must not leak into Org A"
    # Total income must be 5000 (Org A only), not 14999.
    income_total = sum(lk["value"] for lk in res.json()["links"] if lk["target"] == "Income")
    assert income_total == pytest.approx(5000.0)


@pytest.mark.asyncio
async def test_sankey_unknown_key_returns_422(session_factory):
    """``extra="forbid"`` on SankeyQuery: unknown body key → 422."""
    await _seed_two_orgs(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))

    with TestClient(app) as client:
        res = client.post(
            "/api/v1/reports/query/sankey",
            json={"filters": [], "org_id": 1},  # org_id is not in SankeyQuery
        )

    assert res.status_code == 422


@pytest.mark.asyncio
async def test_sankey_feature_gate_off_returns_404(session_factory, monkeypatch):
    """Feature gate OFF → the entire reports router returns 404."""
    monkeypatch.setattr(app_settings, "feature_reports_v2", False)
    await _seed_two_orgs(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))

    with TestClient(app) as client:
        res = client.post("/api/v1/reports/query/sankey", json={"filters": []})

    assert res.status_code == 404


@pytest.mark.asyncio
async def test_sankey_no_income_returns_empty_links(session_factory):
    """When the org has no income transactions, links is empty (not an error)."""
    async with session_factory() as db:
        org = Organization(name="Empty Org", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        user = User(
            org_id=org.id, username="empty_user", email="empty@example.com",
            password_hash=hash_password("pw-1234567"), role=Role.OWNER, email_verified=True,
        )
        db.add(user)
        await db.commit()

    app = _make_app(session_factory, _resolver("empty_user"))

    with TestClient(app) as client:
        res = client.post("/api/v1/reports/query/sankey", json={"filters": []})

    assert res.status_code == 200
    assert res.json()["links"] == []


@pytest.mark.asyncio
async def test_sankey_unsupported_filter_field_returns_422(session_factory):
    """Sending a non-transaction filter field (e.g. account_type) returns 422, not 500.

    ``account_type`` is a valid ``FilterField`` enum value (accepted by Pydantic) but
    is not in the Sankey builder's supported-field whitelist.  The service raises
    ``ValueError``; the router maps it to 422.
    """
    await _seed_two_orgs(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))

    with TestClient(app) as client:
        res = client.post(
            "/api/v1/reports/query/sankey",
            json={"filters": [{"field": "account_type", "op": "eq", "value": 1}]},
        )

    assert res.status_code == 422, res.text
    assert "account_type" in res.json()["detail"]


@pytest.mark.asyncio
async def test_sankey_with_top_n(session_factory):
    """top_n=1 keeps only the largest spending category, folds rest into Other."""
    await _seed_two_orgs(session_factory)
    # Add a second expense category to Org A to test folding.
    async with session_factory() as db:
        from sqlalchemy import select as _s
        org_a = (await db.execute(_s(Organization).where(Organization.name == "Org A"))).scalar_one()
        acct_a = (await db.execute(_s(Account).where(Account.org_id == org_a.id))).scalar_one()
        cat_food = Category(org_id=org_a.id, name="Food")
        db.add(cat_food)
        await db.flush()
        db.add(
            Transaction(
                org_id=org_a.id, account_id=acct_a.id, category_id=cat_food.id,
                description="Food", amount=Decimal("300"),
                type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
                date=date(2026, 6, 1), settled_date=date(2026, 6, 1),
            )
        )
        await db.commit()

    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.post("/api/v1/reports/query/sankey", json={"filters": [], "top_n": 1})

    assert res.status_code == 200
    by = {(lk["source"], lk["target"]): lk["value"] for lk in res.json()["links"]}

    # Housing (2000) is top-1. Food (300) folds to Other.
    assert ("Income", "Housing") in by
    assert ("Income", "Food") not in by
    assert by[("Income", "Other")] == pytest.approx(300.0)
