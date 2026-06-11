"""Tests for GET /api/v1/reports/sources catalog endpoint (Reports v3 Phase 1, Task 5).

Fixture wiring mirrors test_reports.py exactly: in-memory SQLite session factory,
_make_app helper, _resolver helper, and the autouse _enable_flag monkeypatch.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

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
from app.models.user import Organization, Role, User
from app.routers.reports import router as reports_router
from app.security import hash_password


# ─── fixtures (mirrors test_reports.py) ───────────────────────────────────────


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


def _make_app(session_factory, user_resolver):
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_user() -> User:
        return await user_resolver(session_factory)

    def override_factory():
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_session_factory] = override_factory
    app.include_router(reports_router)
    return app


async def _seed(factory) -> None:
    """Minimal seed: one org + one user (owner). No transactions needed for catalog."""
    async with factory() as db:
        org = Organization(name="Test Org", billing_cycle_day=1)
        db.add(org)
        await db.commit()

        user = User(
            org_id=org.id,
            username="test_user",
            email="test@example.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            email_verified=True,
        )
        db.add(user)
        await db.commit()


def _resolver(username: str):
    async def resolve(session_factory):
        async with session_factory() as db:
            from sqlalchemy import select as _s
            return (
                await db.execute(_s(User).where(User.username == username))
            ).scalar_one()
    return resolve


@pytest.fixture(autouse=True)
def _enable_flag(monkeypatch):
    """Default every test in this file to FEATURE_REPORTS_V2 ON."""
    monkeypatch.setattr(app_settings, "feature_reports_v2", True)


# ─── tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sources_endpoint_lists_transactions_catalog(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("test_user"))
    with TestClient(app) as client:
        resp = client.get("/api/v1/reports/sources")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    keys = {s["key"] for s in body}
    assert "transactions" in keys

    tx = next(s for s in body if s["key"] == "transactions")
    assert tx["label"] == "Transactions"

    # Dimensions: must include "category"
    assert any(d["key"] == "category" for d in tx["dimensions"])

    # Every dimension has all required fields
    for d in tx["dimensions"]:
        assert "key" in d
        assert "label" in d
        assert "kind" in d

    # Measures: sum_amount with currency format must be present
    assert any(
        m["key"] == "sum_amount" and m["format"] == "currency"
        for m in tx["measures"]
    )

    # Every measure has all required fields
    for m in tx["measures"]:
        assert "key" in m
        assert "label" in m
        assert "agg" in m
        assert "field" in m
        assert "format" in m


@pytest.mark.asyncio
async def test_sources_endpoint_404_when_flag_off(session_factory, monkeypatch):
    monkeypatch.setattr(app_settings, "feature_reports_v2", False)
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("test_user"))
    with TestClient(app) as client:
        resp = client.get("/api/v1/reports/sources")
    assert resp.status_code == 404
