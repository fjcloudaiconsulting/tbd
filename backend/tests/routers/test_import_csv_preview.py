"""CSV preview endpoint (``POST /api/v1/import/preview``) input bounds.

Mirrors ``test_import_tab.py`` (DB / app fixtures + seeding): the CSV and
TAB routes share one upload scaffold in ``routers/import_router.py``.
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
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
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
from app.rate_limit import limiter
from app.routers.import_router import router as import_router
from app.schemas.import_schemas import MAX_CONFIRM_ROWS
from app.security import hash_password
from app.services.exceptions import ValidationError
from app.services.import_parser import MAX_ROWS

HEADER = "Date;Name / Description;Debit/credit;Amount (EUR)\n"


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


async def _seed(session_factory) -> dict:
    async with session_factory() as db:
        org = Organization(name="CSVTest", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id,
            username=f"csvtester-{org.id}",
            email=f"csv{org.id}@test.example",
            password_hash=hash_password("pw-csv-test-12345"),
            role=Role.OWNER,
            is_active=True,
            email_verified=True,
        )
        atype = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
        groceries = Category(
            org_id=org.id, name="Groceries", slug="groceries",
            is_system=True, type=CategoryType.EXPENSE,
        )
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


def _make_app(session_factory, *, user_id: int) -> FastAPI:
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user() -> User:
        async with session_factory() as db:
            return (await db.execute(select(User).where(User.id == user_id))).scalar_one()

    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_db] = override_get_db

    @app.exception_handler(ValidationError)
    async def _vle(request, exc):
        return JSONResponse(status_code=400, content={"detail": exc.detail})

    app.include_router(import_router)
    return app


def _post_csv(session_factory, seed, payload: bytes):
    app = _make_app(session_factory, user_id=seed["user_id"])
    with TestClient(app, raise_server_exceptions=False) as client:
        return client.post(
            "/api/v1/import/preview",
            files={"file": ("bank.csv", io.BytesIO(payload), "text/csv")},
            data={"account_id": str(seed["account_id"])},
        )


@pytest.mark.asyncio
async def test_csv_preview_happy_path(session_factory):
    seed = await _seed(session_factory)
    resp = _post_csv(
        session_factory, seed,
        (HEADER + "20260406;Groceries;Debit;63,97\n20260407;Salary;Credit;2500,00\n").encode(),
    )
    assert resp.status_code == 200, resp.text
    assert [r["amount"] for r in resp.json()["rows"]] == ["63.97", "2500.00"]


@pytest.mark.asyncio
async def test_csv_preview_unstorable_amount_is_a_row_numbered_400(session_factory):
    """BE-01: rejected at parse, before the preview's SQL detectors run."""
    seed = await _seed(session_factory)
    resp = _post_csv(
        session_factory, seed,
        (HEADER + "20260406;Groceries;Debit;63,97\n20260407;Groceries;Debit;1E+100000\n").encode(),
    )
    assert resp.status_code == 400
    assert resp.json()["detail"].startswith("Row 2: Amount out of range")


@pytest.mark.asyncio
async def test_csv_preview_undecodable_returns_400(session_factory):
    """Bytes that are not UTF-8 are a clean 400, as on the TAB path, not a
    ``UnicodeDecodeError`` 500."""
    seed = await _seed(session_factory)
    resp = _post_csv(session_factory, seed, b"\xff\xfe\x81")
    assert resp.status_code == 400
    assert "utf-8" in resp.json()["detail"].lower()


# ── BE-07: row cap and preview rate limit ──


@pytest.mark.asyncio
async def test_csv_preview_over_the_row_cap_is_a_400_like_tab(session_factory):
    """Same cap and wording as ``.TAB``: the parser stops at row
    ``MAX_ROWS + 1`` instead of previewing (and querying for) every row."""
    seed = await _seed(session_factory)
    rows = "".join("20260406;Groceries;Debit;1,00\n" for _ in range(MAX_ROWS + 1))
    resp = _post_csv(session_factory, seed, (HEADER + rows).encode())
    assert resp.status_code == 400
    assert resp.json()["detail"] == (
        f"Row {MAX_ROWS + 1}: File exceeds the maximum of {MAX_ROWS} transactions"
    )


@pytest.mark.parametrize(
    "path, payload",
    [
        ("/api/v1/import/preview", HEADER.encode()),
        ("/api/v1/import/tab/preview", b"\r\n"),
    ],
)
@pytest.mark.asyncio
async def test_text_previews_are_rate_limited(session_factory, path, payload):
    """30/minute per client, the 31st is a 429. The payloads fail parsing on
    purpose: the limit counts requests, not successful previews."""
    seed = await _seed(session_factory)
    app = _make_app(session_factory, user_id=seed["user_id"])
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    limiter.reset()
    try:
        with TestClient(app) as client:
            def post():
                return client.post(
                    path,
                    files={"file": ("f.txt", io.BytesIO(payload), "text/plain")},
                    data={"account_id": str(seed["account_id"])},
                )

            statuses = [post().status_code for _ in range(30)]
            assert set(statuses) == {400}
            assert post().status_code == 429
    finally:
        limiter.reset()


# ── BE-08: confirm body bounds at the wire ──


def _confirm(session_factory, seed, rows):
    app = _make_app(session_factory, user_id=seed["user_id"])
    with TestClient(app) as client:
        return client.post(
            "/api/v1/import/confirm",
            json={
                "account_id": seed["account_id"], "default_category_id": 1,
                "file_name": "bank.csv", "source_format": "csv", "rows": rows,
            },
        )


def _confirm_row(n, description="Groceries"):
    return {
        "row_number": n, "date": "2026-04-06", "description": description,
        "amount": "1.00", "type": "expense", "skip": True,
    }


@pytest.mark.asyncio
async def test_confirm_with_more_rows_than_any_preview_is_a_422(session_factory):
    seed = await _seed(session_factory)
    resp = _confirm(
        session_factory, seed, [_confirm_row(i) for i in range(MAX_CONFIRM_ROWS + 1)]
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "rows"]


@pytest.mark.asyncio
async def test_confirm_with_an_overlong_description_is_a_422(session_factory):
    seed = await _seed(session_factory)
    resp = _confirm(session_factory, seed, [_confirm_row(1, "x" * 256)])
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "rows", 0, "description"]
    ok = _confirm(session_factory, seed, [_confirm_row(1, "x" * 255)])
    assert ok.status_code == 200, ok.text
