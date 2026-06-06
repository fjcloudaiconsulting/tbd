"""Router-level tests for POST /api/v1/transactions/bulk-update.

Service-level behavior (per-row partial success, transfer gating, tag merge)
is covered in tests/services/test_transaction_service_bulk_update.py. These
tests focus on the HTTP contract:
  - schema validation (extra=forbid, ids bounds, at-least-one-field),
  - happy-path response shape + requested_count dedup,
  - skipped[] surfacing with a reason.
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
from app.models import Account, AccountType, Category, Organization, Transaction
from app.models.base import Base
from app.models.category import CategoryType
from app.models.transaction import TransactionStatus, TransactionType
from app.models.user import Role, User
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
    return app


async def _seed(factory) -> dict:
    async with factory() as db:
        org = Organization(name="Test Org", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id, username="root", email="root@example.com",
            password_hash=hash_password("pw-1234567"), role=Role.OWNER,
            is_superadmin=True, is_active=True, email_verified=True,
        )
        at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
        db.add_all([user, at])
        await db.flush()
        acct = Account(
            org_id=org.id, name="Acct A", account_type_id=at.id,
            balance=Decimal("1000"), currency="EUR",
        )
        db.add(acct)
        await db.flush()
        groceries = Category(
            org_id=org.id, name="Groceries", slug="groceries",
            type=CategoryType.EXPENSE, is_system=False,
        )
        dining = Category(
            org_id=org.id, name="Dining", slug="dining",
            type=CategoryType.EXPENSE, is_system=False,
        )
        db.add_all([groceries, dining])
        await db.flush()
        rows = []
        for i in range(2):
            tx = Transaction(
                org_id=org.id, account_id=acct.id, category_id=groceries.id,
                description=f"row{i}", amount=Decimal("10"),
                type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
                date=date(2026, 5, 1), settled_date=date(2026, 5, 1),
            )
            db.add(tx)
            rows.append(tx)
        await db.commit()
        return {
            "groceries_id": groceries.id,
            "dining_id": dining.id,
            "tx_ids": [r.id for r in rows],
        }


@pytest_asyncio.fixture
async def client(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as c:
        c.seed = seed  # type: ignore[attr-defined]
        yield c


def test_bulk_update_rejects_request_with_no_fields(client):
    seed = client.seed
    r = client.post("/api/v1/transactions/bulk-update", json={"ids": seed["tx_ids"]})
    assert r.status_code == 422


def test_bulk_update_rejects_empty_tags_only(client):
    seed = client.seed
    r = client.post(
        "/api/v1/transactions/bulk-update",
        json={"ids": seed["tx_ids"], "tags": []},
    )
    assert r.status_code == 422


def test_bulk_update_rejects_empty_ids(client):
    r = client.post(
        "/api/v1/transactions/bulk-update",
        json={"ids": [], "category_id": client.seed["dining_id"]},
    )
    assert r.status_code == 422


def test_bulk_update_rejects_unknown_field(client):
    seed = client.seed
    r = client.post(
        "/api/v1/transactions/bulk-update",
        json={"ids": seed["tx_ids"], "category_id": seed["dining_id"], "bogus": 1},
    )
    assert r.status_code == 422


def test_bulk_update_happy_path_response_shape(client):
    seed = client.seed
    r = client.post(
        "/api/v1/transactions/bulk-update",
        json={"ids": seed["tx_ids"], "category_id": seed["dining_id"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "requested_count": 2,
        "updated_count": 2,
        "skipped": [],
    }


def test_bulk_update_requested_count_dedupes(client):
    seed = client.seed
    one = seed["tx_ids"][0]
    r = client.post(
        "/api/v1/transactions/bulk-update",
        json={"ids": [one, one], "category_id": seed["dining_id"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["requested_count"] == 1
    assert body["updated_count"] == 1


def test_bulk_update_reports_skipped_with_reason(client):
    seed = client.seed
    missing = 999999
    r = client.post(
        "/api/v1/transactions/bulk-update",
        json={"ids": [seed["tx_ids"][0], missing], "category_id": seed["dining_id"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["updated_count"] == 1
    assert len(body["skipped"]) == 1
    assert body["skipped"][0]["id"] == missing
    assert isinstance(body["skipped"][0]["reason"], str) and body["skipped"][0]["reason"]
