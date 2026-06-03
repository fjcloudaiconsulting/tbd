"""Router-level test for GET /api/v1/transactions.

Task 2 changed the list endpoint to return a ``ListEnvelope`` (was a bare
list) and to accept ``sort_by``/``sort_dir`` query params that drive
server-side ordering. Invalid sort columns/directions must surface as
HTTP 400 (the service's ``resolve_order_by`` raises ``ValidationError``).

Harness mirrors tests/routers/test_recurring_generate_route.py: an
in-memory SQLite ``session_factory``, ``make_app`` with get_db /
get_current_user overrides, and a ``_seed`` helper that builds an org +
user + account + category + a few transactions of distinct amounts.
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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user
from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.category import CategoryType
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Role, User
from app.routers.transactions import router as transactions_router
from app.security import hash_password


# ── fixtures ───────────────────────────────────────────────────────────────


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
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
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
                await db.execute(_select(User).where(User.is_superadmin.is_(True)))
            ).scalar_one()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user

    app.include_router(transactions_router)
    return app


async def _seed(factory) -> dict:
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
            org_id=org.id, name="Acct A", account_type_id=at.id,
            balance=Decimal("1000"), currency="EUR",
        )
        db.add(acct)
        await db.flush()
        cat = Category(
            org_id=org.id, name="Groceries", slug="groceries",
            type=CategoryType.EXPENSE, is_system=False,
        )
        db.add(cat)
        await db.flush()
        # Distinct amounts so the sort assertion is meaningful, inserted
        # out of order so a default order can't accidentally satisfy it.
        for i, amt in enumerate(["30.00", "10.00", "20.00"]):
            db.add(Transaction(
                org_id=org.id, account_id=acct.id, category_id=cat.id,
                description=f"tx-{i}", amount=Decimal(amt),
                type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
                date=date(2026, 1, i + 1),
                settled_date=date(2026, 1, i + 1),
            ))
        await db.commit()
        return {"org_id": org.id, "acct_id": acct.id, "cat_id": cat.id}


@pytest_asyncio.fixture
async def client(session_factory):
    await _seed(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as c:
        yield c


# ── tests ────────────────────────────────────────────────────────────────


def test_list_returns_envelope(client):
    res = client.get("/api/v1/transactions?limit=2&offset=0")
    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) == {"items", "total", "limit", "offset"}
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert isinstance(body["items"], list)
    assert body["total"] >= len(body["items"])


def test_list_server_side_sort_amount_asc(client):
    res = client.get("/api/v1/transactions?sort_by=amount&sort_dir=asc")
    assert res.status_code == 200
    amounts = [float(i["amount"]) for i in res.json()["items"]]
    assert amounts == sorted(amounts)


def test_invalid_sort_by_is_400(client):
    res = client.get("/api/v1/transactions?sort_by=evil")
    assert res.status_code == 400


def test_invalid_sort_dir_is_400(client):
    res = client.get("/api/v1/transactions?sort_by=amount&sort_dir=up")
    assert res.status_code == 400
