"""Router-level test for the category type-change guard.

Closes the third HIGH finding from PR #150 review: PUT /api/v1/categories/{id}
let a category's `type` be reassigned freely, retroactively breaking every
(type, category) compatibility guard added in the prior commits.

The guard runs only when `body.type` is in the request AND differs from the
current value. Renames and other field updates pass through unchanged.

ValidationError -> 400 (matches existing project convention; see
app/main.py validation_handler and test_category_type_guard_status_codes.py).
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
from app.routers.categories import router as categories_router
from app.security import hash_password


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
    app.include_router(categories_router)
    return app


async def _seed(factory) -> dict:
    async with factory() as db:
        org = Organization(name="T", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id, username="root", email="r@x.com",
            password_hash=hash_password("pw-1234567"), role=Role.OWNER,
            is_superadmin=True, is_active=True, email_verified=True,
        )
        at = AccountType(
            org_id=org.id, name="Checking", slug="checking", is_system=True,
        )
        db.add_all([user, at])
        await db.flush()
        acct = Account(
            org_id=org.id, name="Main", account_type_id=at.id,
            balance=Decimal("0"), currency="EUR",
        )
        expense_master = Category(
            org_id=org.id, name="Groceries", slug="groceries",
            type=CategoryType.EXPENSE,
        )
        both_master = Category(
            org_id=org.id, name="Flex", slug="flex", type=CategoryType.BOTH,
        )
        db.add_all([acct, expense_master, both_master])
        await db.flush()
        # An expense transaction on the BOTH master.
        db.add(Transaction(
            org_id=org.id, account_id=acct.id,
            category_id=both_master.id, description="x",
            amount=Decimal("10"), type=TransactionType.EXPENSE,
            status=TransactionStatus.SETTLED, date=date(2026, 5, 1),
            settled_date=date(2026, 5, 1),
        ))
        await db.commit()
        return {
            "org_id": org.id,
            "expense_master_id": expense_master.id,
            "both_master_id": both_master.id,
        }


@pytest.mark.asyncio
async def test_update_rename_passes_when_type_omitted(session_factory):
    """No type in body: guard is a no-op even with incompatible references."""
    seed = await _seed(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.put(
            f"/api/v1/categories/{seed['both_master_id']}",
            json={"name": "Renamed"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Renamed"


@pytest.mark.asyncio
async def test_update_same_type_passes(session_factory):
    """Body type equals current type: guard is skipped."""
    seed = await _seed(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.put(
            f"/api/v1/categories/{seed['both_master_id']}",
            json={"type": "both"},
        )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_update_to_both_always_safe(session_factory):
    """EXPENSE -> BOTH is always safe regardless of references."""
    seed = await _seed(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.put(
            f"/api/v1/categories/{seed['expense_master_id']}",
            json={"type": "both"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["type"] == "both"


@pytest.mark.asyncio
async def test_update_blocked_by_incompatible_transaction(session_factory):
    """BOTH -> INCOME with an expense txn on the category: 400."""
    seed = await _seed(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.put(
            f"/api/v1/categories/{seed['both_master_id']}",
            json={"type": "income"},
        )
    assert resp.status_code == 400
    detail = resp.json()["detail"].lower()
    assert "category" in detail
    assert "transaction" in detail
