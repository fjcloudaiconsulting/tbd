"""OFX preview endpoint tests (L3.2 Wave 2A).

Covers the implementation behind ``POST /api/v1/import/ofx/preview``:
happy paths (OFX 1.x SGML, OFX 2.x XML), malformed → 400, oversize
file → 413, parse timeout → 400, row-count cap → 413, and a
service-level test that FITID reaches the dedup-key arm.

Fixtures live in ``backend/tests/fixtures/import/ofx/`` and are
synthetic (fictional account numbers + merchants per spec §6.2).
"""
from __future__ import annotations

import asyncio
import datetime
import io
import os
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

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
from app.services import import_ofx_service
from app.services.exceptions import ConflictError, NotFoundError, ValidationError
from app.services.import_ofx_service import parse_ofx
from app.services.import_parser import ParseError, ParsedRow


FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "import" / "ofx"


# ── DB / app fixtures (mirrors test_import_contracts.py) ──


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
    """Seed org + user + account so build_preview can resolve account_id."""
    async with session_factory() as db:
        org = Organization(name="OFXTest", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id,
            username="ofxtester",
            email="ofx@test.example",
            password_hash=hash_password("pw-ofx-test-12345"),
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
        db.add_all([user, atype, groceries])
        await db.flush()
        acct = Account(
            org_id=org.id, account_type_id=atype.id, name="Checking",
            balance=Decimal("0"), currency="EUR",
        )
        db.add(acct)
        await db.commit()
        return {"org_id": org.id, "user_id": user.id, "account_id": acct.id}


def _make_app(session_factory, *, authenticated: bool = True) -> FastAPI:
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    if authenticated:
        async def override_current_user() -> User:
            async with session_factory() as db:
                return (
                    await db.execute(select(User).where(User.is_superadmin.is_(True)))
                ).scalar_one()
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


# ── Happy path: OFX 1.x SGML ──


@pytest.mark.asyncio
async def test_ofx_preview_rabobank_1x_happy_path(session_factory):
    seed = await _seed(session_factory)
    app = _make_app(session_factory)
    payload = _read_fixture("rabobank_ofx_1x.ofx")
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/import/ofx/preview",
            files={"file": ("rabobank.ofx", io.BytesIO(payload), "application/x-ofx")},
            data={"account_id": str(seed["account_id"])},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_rows"] == 15
    assert body["account_id"] == seed["account_id"]
    assert body["file_name"] == "rabobank.ofx"

    # OFX extras must surface on rows (spec §1.3).
    row = body["rows"][0]
    assert row["fitid"] == "RABO000001"
    assert row["bank_id"] == "RABONL2U"
    assert row["account_type_ofx"] == "CHECKING"
    # Sign mapping: -12.50 → expense, amount=|-12.50|.
    assert row["type"] == "expense"
    assert Decimal(row["amount"]) == Decimal("12.50")

    # Salary row → income.
    salary = next(r for r in body["rows"] if r["fitid"] == "RABO000003")
    assert salary["type"] == "income"
    assert Decimal(salary["amount"]) == Decimal("2500.00")


# ── Happy path: OFX 2.x XML ──


@pytest.mark.asyncio
async def test_ofx_preview_ing_2x_happy_path(session_factory):
    seed = await _seed(session_factory)
    app = _make_app(session_factory)
    payload = _read_fixture("ing_ofx_2x.ofx")
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/import/ofx/preview",
            files={"file": ("ing.ofx", io.BytesIO(payload), "application/x-ofx")},
            data={"account_id": str(seed["account_id"])},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_rows"] == 25
    # FITID-aware fields populated on every row.
    for row in body["rows"]:
        assert row["fitid"] is not None
        assert row["bank_id"] == "INGBNL2A"
        assert row["account_type_ofx"] == "CHECKING"


# ── Malformed file → 400 ──


@pytest.mark.asyncio
async def test_ofx_preview_malformed_returns_400(session_factory):
    seed = await _seed(session_factory)
    app = _make_app(session_factory)
    payload = _read_fixture("malformed_truncated.ofx")
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/import/ofx/preview",
            files={"file": ("trunc.ofx", io.BytesIO(payload), "application/x-ofx")},
            data={"account_id": str(seed["account_id"])},
        )
    assert resp.status_code == 400
    body = resp.json()
    assert "ofx" in body["detail"].lower()
    # No stack trace / no PII / no raw file content leaked.
    assert "traceback" not in body["detail"].lower()
    assert "NL01TEST" not in body["detail"]


# ── Oversize file → 413 ──


@pytest.mark.asyncio
async def test_ofx_preview_oversize_returns_413(session_factory):
    """Files > 5 MB are rejected before parse begins (spec §1.2)."""
    seed = await _seed(session_factory)
    app = _make_app(session_factory)
    # 5 MB + 1 byte of pad. Content shape doesn't matter — size check
    # happens *before* parse.
    payload = b"<OFX>" + (b"x" * (5 * 1024 * 1024)) + b"</OFX>"
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/import/ofx/preview",
            files={"file": ("big.ofx", io.BytesIO(payload), "application/x-ofx")},
            data={"account_id": str(seed["account_id"])},
        )
    assert resp.status_code == 413
    assert "too large" in resp.json()["detail"].lower()


# ── Parse timeout → 400 (mocked) ──


@pytest.mark.asyncio
async def test_ofx_preview_timeout_returns_400(session_factory, monkeypatch):
    """A pathological parse that exceeds 10 s surfaces as 400.

    Mocked by stubbing ``_parse_in_executor`` with a function that blocks
    longer than the (shortened) timeout. ``asyncio.wait_for`` raises
    ``TimeoutError`` inside ``parse_ofx``, which the handler maps to
    HTTP 400.
    """
    seed = await _seed(session_factory)
    app = _make_app(session_factory)

    def slow_parse(_raw):
        import time
        time.sleep(0.5)
        return None

    # Use a 0.05 s timeout so the slow_parse stub overruns reliably.
    async def patched_parse_ofx(file_bytes, **kw):
        kw.setdefault("timeout_s", 0.05)
        return await parse_ofx(file_bytes, **kw)

    with patch.object(import_ofx_service, "_parse_in_executor", slow_parse), \
            patch("app.routers.import_router.parse_ofx", patched_parse_ofx):
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/import/ofx/preview",
                files={"file": ("x.ofx", io.BytesIO(b"<OFX>...</OFX>"), "application/x-ofx")},
                data={"account_id": str(seed["account_id"])},
            )
    assert resp.status_code == 400
    assert "complex" in resp.json()["detail"].lower() or "seconds" in resp.json()["detail"].lower()


# ── Row-count cap → 413 ──


@pytest.mark.asyncio
async def test_ofx_preview_too_many_rows_returns_413(session_factory):
    """Files with > 10 000 rows fail post-parse with 413 (spec §1.5)."""
    seed = await _seed(session_factory)
    app = _make_app(session_factory)
    payload = _read_fixture("large_10k_rows.ofx")
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/import/ofx/preview",
            files={"file": ("big.ofx", io.BytesIO(payload), "application/x-ofx")},
            data={"account_id": str(seed["account_id"])},
        )
    assert resp.status_code == 413
    assert "10000" in resp.json()["detail"] or "10001" in resp.json()["detail"]


# ── Auth gate ──


@pytest.mark.asyncio
async def test_ofx_preview_requires_auth(session_factory):
    app = _make_app(session_factory, authenticated=False)
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/import/ofx/preview",
            files={"file": ("x.ofx", io.BytesIO(b"<OFX></OFX>"), "application/x-ofx")},
            data={"account_id": "1"},
        )
    assert resp.status_code == 401


# ── Service-level: FITID extension reaches dedup key (spec §2.1) ──


@pytest.mark.asyncio
async def test_fitid_extension_reaches_dedup_key(session_factory):
    """build_preview marks a second row with the same FITID as duplicate.

    Spec §2.1: when FITID is present, it overrides the description-based
    match. Two rows sharing a FITID in the same upload must collapse,
    even if date / amount / description drift.
    """
    from app.services import import_service

    seed = await _seed(session_factory)
    rows = [
        ParsedRow(
            row_number=1, date=datetime.date(2026, 5, 1),
            description="Original description",
            amount=Decimal("12.50"), type="expense",
            fitid="SHAREDFITID", bank_id="INGBNL2A",
            account_type_ofx="CHECKING",
        ),
        ParsedRow(
            row_number=2, date=datetime.date(2026, 5, 2),  # different date
            description="Drifted description",                # different text
            amount=Decimal("13.00"), type="expense",          # different amount
            fitid="SHAREDFITID", bank_id="INGBNL2A",          # SAME fitid
            account_type_ofx="CHECKING",
        ),
    ]
    async with session_factory() as db:
        result = await import_service.build_preview(
            db,
            org_id=seed["org_id"],
            account_id=seed["account_id"],
            file_name="t.ofx",
            parsed_rows=rows,
        )
    # Second row is the duplicate. FITID hit takes priority over the
    # description-based 5-tuple miss.
    assert result.rows[0].is_duplicate is False
    assert result.rows[1].is_duplicate is True
    assert result.duplicate_count == 1
    # Extras still ride through.
    assert result.rows[0].fitid == "SHAREDFITID"
    assert result.rows[1].bank_id == "INGBNL2A"
    assert result.rows[1].account_type_ofx == "CHECKING"


# ── Service-level: empty TRANLIST → ParseError ──


@pytest.mark.asyncio
async def test_parse_ofx_rejects_empty_tranlist():
    """An OFX file with no <STMTTRN> nodes raises ParseError (spec §1.4)."""
    empty_ofx = b"""<?xml version="1.0" encoding="UTF-8"?>
<?OFX OFXHEADER="200" VERSION="200" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>
<OFX>
  <SIGNONMSGSRSV1><SONRS>
    <STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>
    <DTSERVER>20260501120000</DTSERVER>
    <LANGUAGE>ENG</LANGUAGE>
  </SONRS></SIGNONMSGSRSV1>
  <BANKMSGSRSV1><STMTTRNRS>
    <TRNUID>1</TRNUID>
    <STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>
    <STMTRS>
      <CURDEF>EUR</CURDEF>
      <BANKACCTFROM>
        <BANKID>X</BANKID>
        <ACCTID>NL00TEST</ACCTID>
        <ACCTTYPE>CHECKING</ACCTTYPE>
      </BANKACCTFROM>
      <BANKTRANLIST>
        <DTSTART>20260401</DTSTART>
        <DTEND>20260430</DTEND>
      </BANKTRANLIST>
      <LEDGERBAL><BALAMT>0.00</BALAMT><DTASOF>20260430</DTASOF></LEDGERBAL>
    </STMTRS>
  </STMTTRNRS></BANKMSGSRSV1>
</OFX>"""
    with pytest.raises(ParseError):
        await parse_ofx(empty_ofx)


# ── Service-level: size-cap precondition ──


@pytest.mark.asyncio
async def test_parse_ofx_size_cap_short_circuits():
    """The 5 MB cap is checked before parse begins."""
    from fastapi import HTTPException

    payload = b"x" * (5 * 1024 * 1024 + 1)
    with pytest.raises(HTTPException) as exc_info:
        await parse_ofx(payload)
    assert exc_info.value.status_code == 413
