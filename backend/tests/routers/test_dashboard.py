"""Router tests for GET/PATCH /api/v1/dashboard (W4 Phase 1).

Covers the six required cases:

(a) GET auto-creates a default layout when none exists and returns it.
(b) GET returns the same row on a second call (no duplicate created).
(c) PATCH owner updates layout_json, persists, and the response
    round-trips the full knob-bearing layout VERBATIM (validate-and-
    return-verbatim contract; no silent key stripping).
(d) A second user's GET is isolated — user B never sees user A's layout
    and gets their own default row.
(e) Feature gate OFF → 404 (require_feature raises 404 for gated routes).
(f) Unknown body key in PATCH → 422 (extra="forbid" on DashboardUpdate).
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
from app.routers.dashboard import router as dashboard_router
from app.security import hash_password


# ─── fixtures ─────────────────────────────────────────────────────────────────


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
    app.include_router(dashboard_router)
    return app


async def _seed(factory) -> dict:
    """Two orgs (A + B), one owner user each."""
    async with factory() as db:
        org_a = Organization(name="Org A", billing_cycle_day=1)
        org_b = Organization(name="Org B", billing_cycle_day=1)
        db.add_all([org_a, org_b])
        await db.commit()

        user_a = User(
            org_id=org_a.id,
            username="user_a",
            email="a@example.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            email_verified=True,
        )
        user_b = User(
            org_id=org_b.id,
            username="user_b",
            email="b@example.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            email_verified=True,
        )
        db.add_all([user_a, user_b])
        await db.commit()

        return {
            "org_a_id": org_a.id,
            "org_b_id": org_b.id,
            "user_a_id": user_a.id,
            "user_b_id": user_b.id,
        }


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
    """Default every test in this file to feature_custom_dashboard ON.

    The gate-OFF test flips it back to False explicitly.
    """
    monkeypatch.setattr(app_settings, "feature_custom_dashboard", True)


# ─── (a) GET auto-creates default layout ──────────────────────────────────────


@pytest.mark.asyncio
async def test_get_auto_creates_default_layout(session_factory):
    """First GET for a user who has no row yet auto-creates a default and
    returns it with 200. The returned body includes id, owner_user_id,
    org_id, layout_json, canvas_filters_json, schema_version.
    """
    seeds = await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.get("/api/v1/dashboard")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["owner_user_id"] == seeds["user_a_id"]
    assert body["org_id"] == seeds["org_a_id"]
    assert "layout_json" in body
    assert "canvas_filters_json" in body
    assert body["schema_version"] == 1
    assert body["id"] is not None


# ─── (b) Second GET returns the same row (no duplicate) ───────────────────────


@pytest.mark.asyncio
async def test_get_returns_same_row_on_second_call(session_factory):
    """Two successive GETs return the same dashboard row (same id, no
    duplicate insert violating the UNIQUE owner constraint).
    """
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res1 = client.get("/api/v1/dashboard")
        res2 = client.get("/api/v1/dashboard")
    assert res1.status_code == 200
    assert res2.status_code == 200
    assert res1.json()["id"] == res2.json()["id"]


# ─── (c) PATCH updates + persists + round-trips verbatim ──────────────────────


@pytest.mark.asyncio
async def test_patch_updates_layout_round_trips_verbatim(session_factory):
    """PATCH with a knob-bearing layout_json persists to DB and the response
    round-trips it VERBATIM (no silent key stripping — validate-and-return-
    verbatim pattern, same as #424 fix in reports).
    """
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))

    knob_layout = {
        "version": 1,
        "widgets": [
            {
                "id": "w1",
                "type": "kpi",
                "title": "Total Spent",
                "grid": {"x": 0, "y": 0, "w": 2, "h": 2},
                "config": {
                    "dataset": "transactions",
                    "measure": {"agg": "sum", "field": "amount"},
                    # visual knobs that must NOT be silently stripped
                    "compare_prior_period": True,
                    "top_n": 10,
                },
            }
        ],
    }

    with TestClient(app) as client:
        # ensure row exists first
        client.get("/api/v1/dashboard")
        res = client.patch(
            "/api/v1/dashboard",
            json={"layout_json": knob_layout},
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["layout_json"] == knob_layout

    # Verify persistence: a fresh GET returns the updated layout
    app2 = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app2) as client2:
        res2 = client2.get("/api/v1/dashboard")
    assert res2.status_code == 200
    assert res2.json()["layout_json"] == knob_layout


# ─── (d) Per-user scoping — user B never sees user A's layout ─────────────────


@pytest.mark.asyncio
async def test_get_is_isolated_per_user(session_factory):
    """User A and User B each get their own dashboard row.  User B's GET
    must NOT return user A's layout (different owner_user_id rows).
    """
    seeds = await _seed(session_factory)

    # User A creates + customises their layout first
    app_a = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app_a) as client_a:
        client_a.get("/api/v1/dashboard")
        client_a.patch(
            "/api/v1/dashboard",
            json={"layout_json": {"version": 1, "widgets": [{"id": "only-for-a"}]}},
        )

    # User B's GET should return their own (auto-created) row, not A's
    app_b = _make_app(session_factory, _resolver("user_b"))
    with TestClient(app_b) as client_b:
        res_b = client_b.get("/api/v1/dashboard")
    assert res_b.status_code == 200
    body_b = res_b.json()
    assert body_b["owner_user_id"] == seeds["user_b_id"]
    # Must NOT contain A's widget
    widgets = body_b["layout_json"].get("widgets", [])
    widget_ids = [w.get("id") for w in widgets]
    assert "only-for-a" not in widget_ids


# ─── (e) Feature gate OFF → 404 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_feature_gate_off_returns_404_on_get(session_factory, monkeypatch):
    monkeypatch.setattr(app_settings, "feature_custom_dashboard", False)
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.get("/api/v1/dashboard")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_feature_gate_off_returns_404_on_patch(session_factory, monkeypatch):
    monkeypatch.setattr(app_settings, "feature_custom_dashboard", False)
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.patch("/api/v1/dashboard", json={"layout_json": {}})
    assert res.status_code == 404


# ─── (f) Unknown body key → 422 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_unknown_key_returns_422(session_factory):
    """DashboardUpdate uses extra='forbid'; an unknown key must be rejected
    with 422 Unprocessable Entity.
    """
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        # ensure row exists
        client.get("/api/v1/dashboard")
        res = client.patch(
            "/api/v1/dashboard",
            json={"layout_json": {}, "unknown_key": "bad"},
        )
    assert res.status_code == 422
