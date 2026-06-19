"""Feature gate integration tests — Reports + Scenarios routers.

Contract: ``GET /api/v1/reports`` and ``GET /api/v1/scenarios`` return 404
when their feature resolves to off via ``require_feature``, and 200 when on.
Resolution priority: org-level DB override beats the env floor in both
directions (on-beats-false-env, off-beats-true-env).
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
from app.models.settings import OrgSetting
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

    @event.listens_for(engine.sync_engine, "connect")
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


# ---------------------------------------------------------------------------
# DB-override gate tests — org override beats env floor (both directions)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reports_org_override_off_beats_true_env(session_factory, monkeypatch):
    """Org-level OrgSetting 'off' beats a True env floor: router returns 404.

    env feature_reports_v2=True but org has key='feature.reports', value='off'
    → require_feature resolves 'off' → GET /api/v1/reports returns 404.
    """
    monkeypatch.setattr(app_settings, "feature_reports_v2", True)
    ids = await _seed(session_factory)

    # Seed the org-level override that turns the feature off
    async with session_factory() as db:
        db.add(OrgSetting(org_id=ids["org_id"], key="feature.reports", value="off"))
        await db.commit()

    app = _make_reports_app(session_factory, _resolve_user)
    with TestClient(app) as client:
        res = client.get("/api/v1/reports")
    assert res.status_code == 404, (
        f"Expected 404 (org override 'off' beats True env), got {res.status_code}"
    )


@pytest.mark.asyncio
async def test_scenarios_org_override_on_beats_false_env(session_factory, monkeypatch):
    """Org-level OrgSetting 'on' beats a False env floor: router returns 200.

    env feature_plans=False but org has key='feature.plans', value='on'
    → require_feature resolves 'on' → GET /api/v1/scenarios returns 200.
    """
    monkeypatch.setattr(app_settings, "feature_plans", False)
    ids = await _seed(session_factory)

    # Seed the org-level override that turns the feature on
    async with session_factory() as db:
        db.add(OrgSetting(org_id=ids["org_id"], key="feature.plans", value="on"))
        await db.commit()

    app = _make_scenarios_app(session_factory, _resolve_user)
    with TestClient(app) as client:
        res = client.get("/api/v1/scenarios")
    assert res.status_code == 200, (
        f"Expected 200 (org override 'on' beats False env), got {res.status_code}"
    )
    assert res.json() == []
