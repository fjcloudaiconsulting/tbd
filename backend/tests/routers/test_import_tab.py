"""ABN AMRO ``.TAB`` preview endpoint tests.

Covers ``POST /api/v1/import/tab/preview``: happy path against the
synthetic fixture, auth required, org-scoping, and a bad file → 400.
Mirrors ``test_import_ofx.py`` (DB / app fixtures + seeding).

Fixture lives in ``backend/tests/fixtures/import/tab/`` and is synthetic
(fabricated IBANs / names / amounts per spec).

Spec: ``specs/2026-06-09-abn-tab-import.md``.
"""
from __future__ import annotations

import io
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse
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
from app.deps import get_current_user
from app.models import Base
from app.models.account import Account, AccountType
from app.models.category import Category, CategoryType
from app.models.user import Organization, Role, User
from app.routers.import_router import router as import_router
from app.security import hash_password
from app.services.exceptions import ConflictError, NotFoundError, ValidationError


FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "import" / "tab"


# ── DB / app fixtures (mirrors test_import_ofx.py) ──


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(Engine, "connect")
    def _fk_on(dbapi_conn, _r):
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


async def _seed(session_factory, *, org_name: str = "TABTest") -> dict:
    """Seed org + user + account so build_preview can resolve account_id."""
    async with session_factory() as db:
        org = Organization(name=org_name, billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id,
            username=f"tabtester-{org.id}",
            email=f"tab{org.id}@test.example",
            password_hash=hash_password("pw-tab-test-12345"),
            role=Role.OWNER,
            is_superadmin=True,
            is_active=True,
            email_verified=True,
        )
        atype = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
        groceries = Category(
            org_id=org.id, name="Groceries", slug="groceries",
            is_system=True, type=CategoryType.EXPENSE,
        )
        # Layer B preflight needs an income-compatible category because the
        # fixture contains a salary (credit) row.
        income = Category(
            org_id=org.id, name="Salary", slug="salary",
            is_system=True, type=CategoryType.INCOME,
        )
        db.add_all([user, atype, groceries, income])
        await db.flush()
        acct = Account(
            org_id=org.id, account_type_id=atype.id, name="Checking",
            balance=Decimal("0"), currency="EUR",
        )
        db.add(acct)
        await db.commit()
        return {"org_id": org.id, "user_id": user.id, "account_id": acct.id}


def _make_app(session_factory, *, user_id: int | None = None, authenticated: bool = True) -> FastAPI:
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    if authenticated:
        async def override_current_user() -> User:
            async with session_factory() as db:
                if user_id is not None:
                    return (
                        await db.execute(select(User).where(User.id == user_id))
                    ).scalar_one()
                return (
                    await db.execute(select(User).where(User.is_superadmin.is_(True)))
                ).scalars().first()
        app.dependency_overrides[get_current_user] = override_current_user
    else:
        from fastapi import HTTPException

        async def reject_user():
            raise HTTPException(status_code=401, detail="not authenticated")
        app.dependency_overrides[get_current_user] = reject_user

    app.dependency_overrides[get_db] = override_get_db

    @app.exception_handler(NotFoundError)
    async def _nfe(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    async def _vle(request, exc):
        return JSONResponse(status_code=400, content={"detail": exc.detail})

    @app.exception_handler(ConflictError)
    async def _cfe(request, exc):
        return JSONResponse(status_code=409, content={"detail": exc.detail})

    app.include_router(import_router)
    return app


def _read_fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ── Happy path ──


@pytest.mark.asyncio
async def test_tab_preview_happy_path(session_factory):
    seed = await _seed(session_factory)
    app = _make_app(session_factory, user_id=seed["user_id"])
    payload = _read_fixture("abn_sample.tab")
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/import/tab/preview",
            files={"file": ("abn_sample.tab", io.BytesIO(payload), "text/plain")},
            data={"account_id": str(seed["account_id"])},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_rows"] == 6
    assert body["account_id"] == seed["account_id"]
    assert body["file_name"] == "abn_sample.tab"
    assert body["source_format"] == "tab"

    # SEPA debit row: -11,71 → expense, amount=|11.71|, NAME → counterparty.
    first = body["rows"][0]
    assert first["type"] == "expense"
    assert Decimal(first["amount"]) == Decimal("11.71")
    assert first["counterparty"] == "WATER COMPANY BV"

    # Salary credit row → income.
    salary = next(r for r in body["rows"] if r["counterparty"] == "EXAMPLE EMPLOYER NV")
    assert salary["type"] == "income"
    assert Decimal(salary["amount"]) == Decimal("2500.00")

    # iDEAL/Wero row: the extra unpaired ``Wero`` token must NOT prevent
    # NAME extraction (the headline bug this PR fixed), exercised end-to-end
    # through the endpoint with a slash-bearing REMI value.
    ideal = next(r for r in body["rows"] if r["counterparty"] == "Online Shop B.V.")
    assert ideal["type"] == "expense"
    assert Decimal(ideal["amount"]) == Decimal("20.00")


# ── Auth gate ──


@pytest.mark.asyncio
async def test_tab_preview_requires_auth(session_factory):
    app = _make_app(session_factory, authenticated=False)
    payload = _read_fixture("abn_sample.tab")
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/import/tab/preview",
            files={"file": ("abn_sample.tab", io.BytesIO(payload), "text/plain")},
            data={"account_id": "1"},
        )
    assert resp.status_code == 401


# ── Org-scoping: account in another org → not found ──


@pytest.mark.asyncio
async def test_tab_preview_org_scoped_account(session_factory):
    """A user cannot preview into an account owned by another org.

    ``build_preview`` validates the destination account by
    ``(id, org_id)``; a cross-org account surfaces the same
    ``ValidationError("Invalid account")`` → 400 as the CSV / OFX paths
    (``transaction_service.validate_account``). The org scope is enforced
    regardless of the status code.
    """
    org_a = await _seed(session_factory, org_name="OrgA")
    org_b = await _seed(session_factory, org_name="OrgB")
    # Authenticate as org A's user, but target org B's account.
    app = _make_app(session_factory, user_id=org_a["user_id"])
    payload = _read_fixture("abn_sample.tab")
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/import/tab/preview",
            files={"file": ("abn_sample.tab", io.BytesIO(payload), "text/plain")},
            data={"account_id": str(org_b["account_id"])},
        )
    assert resp.status_code == 400
    assert "account" in resp.json()["detail"].lower()


# ── Bad file → 400 ──


@pytest.mark.asyncio
async def test_tab_preview_malformed_returns_400(session_factory):
    seed = await _seed(session_factory)
    app = _make_app(session_factory, user_id=seed["user_id"])
    # Wrong field count on the only line.
    payload = b"only\tthree\tfields\n"
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/import/tab/preview",
            files={"file": ("bad.tab", io.BytesIO(payload), "text/plain")},
            data={"account_id": str(seed["account_id"])},
        )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "Row 1" in detail


@pytest.mark.asyncio
async def test_tab_preview_oversize_returns_400(session_factory):
    """Files > 5 MB are rejected (ValidationError → 400, mirrors CSV path)."""
    seed = await _seed(session_factory)
    app = _make_app(session_factory, user_id=seed["user_id"])
    payload = b"x" * (5 * 1024 * 1024 + 1)
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/import/tab/preview",
            files={"file": ("big.tab", io.BytesIO(payload), "text/plain")},
            data={"account_id": str(seed["account_id"])},
        )
    assert resp.status_code == 400
    assert "too large" in resp.json()["detail"].lower()
