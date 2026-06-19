"""Feature gate integration tests — Reports + Scenarios routers.

TDD: these tests are written BEFORE the router changes are made.

Expected RED state:
- Reports gate tests: ``test_reports_gate_off_returns_404`` will PASS
  (the old ``require_reports_v2_enabled`` also 404s when the env floor
  is False) but ``test_reports_gate_on_returns_200`` will also PASS
  because the old dep reads ``feature_reports_v2`` directly.
  → Still valid as a regression pin once we migrate to require_feature.
- Scenarios gate tests: ``test_scenarios_gate_off_returns_404`` will FAIL
  (RED) because scenarios is currently UNGATED — it returns 200 when the
  feature is off. ``test_scenarios_gate_on_returns_200`` will PASS.

After implementation (GREEN):
- All four tests pass.
- unauthenticated ``/reports`` and ``/scenarios`` now return 401/403
  (auth fires before the gate) rather than 404 — that is the accepted
  behaviour for org-scoped gating and is NOT tested here (it is already
  covered by test_reports.py::test_anonymous_request_returns_401).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from datetime import date

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
from app.models.user import Organization, Role, User
from app.routers.reports import router as reports_router
from app.routers.scenarios import router as scenarios_router
from app.security import hash_password


# ---------------------------------------------------------------------------
# Shared in-memory SQLite fixture
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Seed + app helpers
# ---------------------------------------------------------------------------


async def _seed(factory) -> dict:
    async with factory() as db:
        org = Organization(name="Gate Test Org", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id,
            username="gateuser",
            email="gate@example.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            is_superadmin=False,
            is_active=True,
            email_verified=True,
        )
        db.add(user)
        await db.flush()
        at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
        db.add(at)
        await db.flush()
        acc = Account(
            org_id=org.id,
            account_type_id=at.id,
            name="Main",
            balance=Decimal("1000.00"),
            currency="EUR",
            is_active=True,
            is_default=True,
            opening_balance=Decimal("1000.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        db.add(acc)
        await db.commit()
        return {"org_id": org.id, "user_id": user.id}


def _make_reports_app(factory, user_resolver):
    app = FastAPI()

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with factory() as s:
            yield s

    async def override_user() -> User:
        return await user_resolver(factory)

    def override_factory():
        return factory

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_session_factory] = override_factory
    app.include_router(reports_router)
    return app


def _make_scenarios_app(factory, user_resolver):
    app = FastAPI()

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with factory() as s:
            yield s

    async def override_user() -> User:
        return await user_resolver(factory)

    def override_factory():
        return factory

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_session_factory] = override_factory
    app.include_router(scenarios_router)
    return app


async def _resolve_user(factory):
    from sqlalchemy import select
    async with factory() as db:
        return (await db.execute(select(User).where(User.username == "gateuser"))).scalar_one()


# ---------------------------------------------------------------------------
# Reports gate tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reports_gate_off_returns_404(session_factory, monkeypatch):
    """GET /api/v1/reports returns 404 when feature_reports_v2 env floor is False
    and there are no DB override rows."""
    monkeypatch.setattr(app_settings, "feature_reports_v2", False)
    await _seed(session_factory)
    app = _make_reports_app(session_factory, _resolve_user)
    with TestClient(app) as client:
        res = client.get("/api/v1/reports")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_reports_gate_on_returns_200(session_factory, monkeypatch):
    """GET /api/v1/reports returns 200 (empty list) when feature_reports_v2 env floor is True."""
    monkeypatch.setattr(app_settings, "feature_reports_v2", True)
    await _seed(session_factory)
    app = _make_reports_app(session_factory, _resolve_user)
    with TestClient(app) as client:
        res = client.get("/api/v1/reports")
    assert res.status_code == 200
    assert res.json() == []


# ---------------------------------------------------------------------------
# Scenarios gate tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenarios_gate_off_returns_404(session_factory, monkeypatch):
    """GET /api/v1/scenarios returns 404 when feature_plans env floor is False
    and there are no DB override rows.

    RED before implementation: scenarios is ungated so this currently returns 200.
    GREEN after: require_feature(Feature.PLANS) on the router 404s when off.
    """
    monkeypatch.setattr(app_settings, "feature_plans", False)
    await _seed(session_factory)
    app = _make_scenarios_app(session_factory, _resolve_user)
    with TestClient(app) as client:
        res = client.get("/api/v1/scenarios")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_scenarios_gate_on_returns_200(session_factory, monkeypatch):
    """GET /api/v1/scenarios returns 200 (empty list) when feature_plans env floor is True."""
    monkeypatch.setattr(app_settings, "feature_plans", True)
    await _seed(session_factory)
    app = _make_scenarios_app(session_factory, _resolve_user)
    with TestClient(app) as client:
        res = client.get("/api/v1/scenarios")
    assert res.status_code == 200
    assert res.json() == []
