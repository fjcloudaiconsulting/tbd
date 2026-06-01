"""Router-level test for POST /api/v1/recurring/generate.

Task 2 changed ``generate_due_transactions`` to return a summary dict
``{"generated", "settled", "pending", "period_end"}`` instead of a bare int.
This test pins the route handler returning that dict through unchanged
(rather than wrapping it in ``{"generated": <dict>}``).

The fixture wiring mirrors
tests/routers/test_transactions_promote_to_recurring.py: an in-memory
SQLite ``session_factory``, ``make_app`` with get_db / get_current_user
overrides, and a ``_seed`` helper that builds an org + user + account +
category.
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
from app.models.recurring import Frequency, RecurringTransaction
from app.models.user import Role, User
from app.routers.recurring import router as recurring_router
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

    app.include_router(recurring_router)
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
        # An ACTIVE template due today (or earlier) is always within the
        # current cycle's window so the route has something to generate.
        tmpl = RecurringTransaction(
            org_id=org.id, account_id=acct.id, category_id=cat.id,
            description="Rent", amount=Decimal("100"), type="expense",
            frequency=Frequency.MONTHLY, next_due_date=date.today(),
            auto_settle=False, is_active=True,
        )
        db.add(tmpl)
        await db.commit()
        return {"org_id": org.id, "acct_id": acct.id, "cat_id": cat.id}


# ── generate returns the summary dict ───────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_returns_summary_dict(session_factory):
    await _seed(session_factory)

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post("/api/v1/recurring/generate")

    assert res.status_code == 200, res.text
    body = res.json()
    assert set(["generated", "settled", "pending", "period_end"]).issubset(body.keys())
    assert isinstance(body["generated"], int)
    assert isinstance(body["period_end"], str)
