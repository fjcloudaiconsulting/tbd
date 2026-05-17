"""Layer B wire-shape test: POST /api/v1/import/preview returns a
structured 400 when the org is missing a category type that the parsed
rows require.

The frontend reads ``detail.code`` and ``detail.missing_types`` to render
a deep-link to /categories. Both fields are part of the contract.
"""
from __future__ import annotations

import io
from collections.abc import AsyncIterator
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
from app.models import Base
from app.models.account import Account, AccountType
from app.models.category import Category, CategoryType
from app.models.user import Organization, Role, User
from app.routers.import_router import router as import_router
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


async def _seed(factory, *, category_types: list[CategoryType]) -> dict:
    async with factory() as db:
        org = Organization(name="MCT", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id, username="o", email="o@x.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER, is_superadmin=True, is_active=True,
            email_verified=True,
        )
        atype = AccountType(
            org_id=org.id, name="Checking", slug="checking", is_system=True,
        )
        db.add_all([user, atype])
        await db.flush()
        acct = Account(
            org_id=org.id, account_type_id=atype.id, name="Chk",
            balance=Decimal("0"), currency="EUR",
        )
        db.add(acct)
        for i, ct in enumerate(category_types):
            db.add(Category(
                org_id=org.id, name=f"C{i}", slug=f"c_{ct.value}_{i}",
                type=ct, is_system=False,
            ))
        await db.commit()
        return {"org_id": org.id, "account_id": acct.id}


def _make_app(factory) -> FastAPI:
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    async def override_current_user() -> User:
        from sqlalchemy import select
        async with factory() as db:
            return (
                await db.execute(select(User).where(User.is_superadmin.is_(True)))
            ).scalar_one()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user

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


# Minimal valid ING-style CSV with a single expense row.
EXPENSE_CSV = (
    "Date;Name / Description;Account;Counterparty;Code;Debit/credit;"
    "Amount (EUR);Transaction type;Notifications\n"
    "20260510;Albert Heijn;NL01TEST;NL02OTHER;BA;Debit;12,50;Payment;"
    "Groceries\n"
)
INCOME_CSV = (
    "Date;Name / Description;Account;Counterparty;Code;Debit/credit;"
    "Amount (EUR);Transaction type;Notifications\n"
    "20260510;Salary;NL01TEST;NL02OTHER;BA;Credit;2500,00;Online Banking;"
    "Salary\n"
)


@pytest.mark.asyncio
async def test_expense_csv_missing_expense_category_returns_structured_400(
    session_factory,
) -> None:
    seed = await _seed(session_factory, category_types=[CategoryType.INCOME])
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/import/preview",
            files={"file": ("t.csv", io.BytesIO(EXPENSE_CSV.encode()), "text/csv")},
            data={"account_id": str(seed["account_id"])},
        )
    assert res.status_code == 400, res.text
    detail = res.json()["detail"]
    assert detail["code"] == "missing_category_type"
    assert detail["missing_types"] == ["expense"]
    assert isinstance(detail["message"], str)
    assert detail["message"]


@pytest.mark.asyncio
async def test_income_csv_missing_income_category_returns_structured_400(
    session_factory,
) -> None:
    seed = await _seed(session_factory, category_types=[CategoryType.EXPENSE])
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/import/preview",
            files={"file": ("t.csv", io.BytesIO(INCOME_CSV.encode()), "text/csv")},
            data={"account_id": str(seed["account_id"])},
        )
    assert res.status_code == 400, res.text
    detail = res.json()["detail"]
    assert detail["code"] == "missing_category_type"
    assert detail["missing_types"] == ["income"]


@pytest.mark.asyncio
async def test_csv_with_both_typed_category_passes(session_factory) -> None:
    """A single ``BOTH`` category satisfies the preflight for expense rows."""
    seed = await _seed(session_factory, category_types=[CategoryType.BOTH])
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/import/preview",
            files={"file": ("t.csv", io.BytesIO(EXPENSE_CSV.encode()), "text/csv")},
            data={"account_id": str(seed["account_id"])},
        )
    assert res.status_code == 200, res.text
