"""Tests for the superadmin global + per-org feature gate endpoints.

TDD: RED first (router not yet registered), then GREEN after implementation.

Covers:
- Non-superadmin (OWNER) → 403 on global PUT and per-org PUT
- Superadmin PUT global `plans=on` → GET shows `global_value: "on"` + env_floor present
- PUT global `plans=inherit` → row deleted → `global_value: null`
- Per-org PUT `reports=on` → GET shows `override: "on"`, `effective: true`
- Each successful write produces an `audit_events` row with right event_type
- Unknown feature name → 404
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
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
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.system_setting import SystemSetting
from app.models.settings import OrgSetting
from app.models.user import Organization, Role, User
from app.routers.admin_features import router as admin_features_router
from app.security import hash_password


# ─── fixtures ─────────────────────────────────────────────────────


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


async def _seed(factory: async_sessionmaker[AsyncSession]) -> dict:
    """Seed: one regular org+owner, one superadmin user."""
    async with factory() as db:
        org = Organization(name="Test Org", billing_cycle_day=1)
        db.add(org)
        await db.commit()

        owner = User(
            org_id=org.id,
            username="owner",
            email="owner@example.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            email_verified=True,
            is_superadmin=False,
        )
        superadmin = User(
            org_id=org.id,
            username="superadmin",
            email="super@example.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            email_verified=True,
            is_superadmin=True,
        )
        db.add_all([owner, superadmin])
        await db.commit()

        return {
            "org_id": org.id,
            "owner_id": owner.id,
            "superadmin_id": superadmin.id,
        }


def _make_app(
    session_factory: async_sessionmaker[AsyncSession],
    user_resolver,
) -> FastAPI:
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_user(request: Request) -> User:
        request.state.auth_method = "jwt"  # interactive-session guard (spec §7)
        return await user_resolver(session_factory)

    def override_factory():
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_session_factory] = override_factory
    app.include_router(admin_features_router)
    return app


# ─── helpers ──────────────────────────────────────────────────────


async def _get_user_by_id(factory, user_id: int) -> User:
    async with factory() as db:
        return await db.get(User, user_id)


async def _count_audit_events(factory, event_type: str) -> int:
    async with factory() as db:
        rows = await db.scalars(
            select(AuditEvent).where(AuditEvent.event_type == event_type)
        )
        return len(list(rows))


async def _latest_audit_event(factory, event_type: str) -> AuditEvent | None:
    """Return the most recently inserted AuditEvent with the given event_type."""
    async with factory() as db:
        return await db.scalar(
            select(AuditEvent)
            .where(AuditEvent.event_type == event_type)
            .order_by(AuditEvent.id.desc())
            .limit(1)
        )


# ─── tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_superadmin_global_put_returns_403(session_factory):
    """Regular OWNER must be rejected with 403 on global PUT."""
    ids = await _seed(session_factory)

    async def owner_resolver(factory):
        return await _get_user_by_id(factory, ids["owner_id"])

    app = _make_app(session_factory, owner_resolver)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.put(
        "/api/v1/admin/features/plans",
        json={"value": "on"},
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_non_superadmin_per_org_put_returns_403(session_factory):
    """Regular OWNER must be rejected with 403 on per-org PUT."""
    ids = await _seed(session_factory)

    async def owner_resolver(factory):
        return await _get_user_by_id(factory, ids["owner_id"])

    app = _make_app(session_factory, owner_resolver)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.put(
        f"/api/v1/admin/orgs/{ids['org_id']}/features/reports",
        json={"value": "on"},
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_superadmin_global_get_returns_all_features(session_factory):
    """GET /admin/features lists all Feature enum values."""
    ids = await _seed(session_factory)

    async def superadmin_resolver(factory):
        return await _get_user_by_id(factory, ids["superadmin_id"])

    app = _make_app(session_factory, superadmin_resolver)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get("/api/v1/admin/features")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    feature_names = {item["feature"] for item in data}
    assert "reports" in feature_names
    assert "plans" in feature_names
    # env_floor must be present and boolean
    for item in data:
        assert "env_floor" in item
        assert isinstance(item["env_floor"], bool)


@pytest.mark.asyncio
async def test_superadmin_put_global_on_then_get_shows_on(session_factory):
    """PUT plans=on → GET shows global_value='on'."""
    ids = await _seed(session_factory)

    async def superadmin_resolver(factory):
        return await _get_user_by_id(factory, ids["superadmin_id"])

    app = _make_app(session_factory, superadmin_resolver)
    client = TestClient(app, raise_server_exceptions=True)

    put_resp = client.put(
        "/api/v1/admin/features/plans",
        json={"value": "on"},
    )
    assert put_resp.status_code == 200, put_resp.text
    result = put_resp.json()
    assert result["feature"] == "plans"
    assert result["global_value"] == "on"
    assert "env_floor" in result

    get_resp = client.get("/api/v1/admin/features")
    assert get_resp.status_code == 200
    plans = next(i for i in get_resp.json() if i["feature"] == "plans")
    assert plans["global_value"] == "on"


@pytest.mark.asyncio
async def test_superadmin_put_global_inherit_deletes_row(session_factory):
    """PUT plans=on then PUT plans=inherit → global_value is null."""
    ids = await _seed(session_factory)

    async def superadmin_resolver(factory):
        return await _get_user_by_id(factory, ids["superadmin_id"])

    app = _make_app(session_factory, superadmin_resolver)
    client = TestClient(app, raise_server_exceptions=True)

    client.put("/api/v1/admin/features/plans", json={"value": "on"})

    put_resp = client.put(
        "/api/v1/admin/features/plans",
        json={"value": "inherit"},
    )
    assert put_resp.status_code == 200, put_resp.text
    result = put_resp.json()
    assert result["global_value"] is None

    get_resp = client.get("/api/v1/admin/features")
    plans = next(i for i in get_resp.json() if i["feature"] == "plans")
    assert plans["global_value"] is None


@pytest.mark.asyncio
async def test_superadmin_per_org_put_reports_on(session_factory):
    """Per-org PUT reports=on → override='on', effective=True."""
    ids = await _seed(session_factory)

    async def superadmin_resolver(factory):
        return await _get_user_by_id(factory, ids["superadmin_id"])

    app = _make_app(session_factory, superadmin_resolver)
    client = TestClient(app, raise_server_exceptions=True)

    org_id = ids["org_id"]
    put_resp = client.put(
        f"/api/v1/admin/orgs/{org_id}/features/reports",
        json={"value": "on"},
    )
    assert put_resp.status_code == 200, put_resp.text
    result = put_resp.json()
    assert result["feature"] == "reports"
    assert result["override"] == "on"
    assert result["effective"] is True

    get_resp = client.get(f"/api/v1/admin/orgs/{org_id}/features")
    assert get_resp.status_code == 200
    reports = next(i for i in get_resp.json() if i["feature"] == "reports")
    assert reports["override"] == "on"
    assert reports["effective"] is True


@pytest.mark.asyncio
async def test_per_org_get_unknown_org_returns_404(session_factory):
    """GET /admin/orgs/9999/features → 404 when org doesn't exist."""
    ids = await _seed(session_factory)

    async def superadmin_resolver(factory):
        return await _get_user_by_id(factory, ids["superadmin_id"])

    app = _make_app(session_factory, superadmin_resolver)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get("/api/v1/admin/orgs/9999/features")
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_per_org_put_unknown_org_returns_404(session_factory):
    """PUT /admin/orgs/9999/features/reports → 404; no side effects."""
    ids = await _seed(session_factory)

    async def superadmin_resolver(factory):
        return await _get_user_by_id(factory, ids["superadmin_id"])

    app = _make_app(session_factory, superadmin_resolver)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.put(
        "/api/v1/admin/orgs/9999/features/reports",
        json={"value": "on"},
    )
    assert resp.status_code == 404, resp.text

    # No OrgSetting row should exist for org 9999
    async with session_factory() as db:
        row = await db.scalar(
            select(OrgSetting).where(OrgSetting.org_id == 9999)
        )
    assert row is None, "PUT to unknown org must not create an OrgSetting row"

    # No feature.org.set audit row should exist for org 9999
    async with session_factory() as db:
        audit_row = await db.scalar(
            select(AuditEvent).where(
                AuditEvent.event_type == "feature.org.set",
                AuditEvent.target_org_id == 9999,
            )
        )
    assert audit_row is None, "PUT to unknown org must not write a feature.org.set audit row"


@pytest.mark.asyncio
async def test_unknown_feature_name_global_put_returns_404(session_factory):
    """PUT /admin/features/unknown → 404."""
    ids = await _seed(session_factory)

    async def superadmin_resolver(factory):
        return await _get_user_by_id(factory, ids["superadmin_id"])

    app = _make_app(session_factory, superadmin_resolver)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.put(
        "/api/v1/admin/features/unknown_feature",
        json={"value": "on"},
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_unknown_feature_name_per_org_put_returns_404(session_factory):
    """PUT /admin/orgs/{id}/features/unknown → 404."""
    ids = await _seed(session_factory)

    async def superadmin_resolver(factory):
        return await _get_user_by_id(factory, ids["superadmin_id"])

    app = _make_app(session_factory, superadmin_resolver)
    client = TestClient(app, raise_server_exceptions=True)

    org_id = ids["org_id"]
    resp = client.put(
        f"/api/v1/admin/orgs/{org_id}/features/unknown_feature",
        json={"value": "on"},
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_non_superadmin_global_get_returns_403(session_factory):
    """Regular OWNER must be rejected with 403 on GET /admin/features."""
    ids = await _seed(session_factory)

    async def owner_resolver(factory):
        return await _get_user_by_id(factory, ids["owner_id"])

    app = _make_app(session_factory, owner_resolver)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get("/api/v1/admin/features")
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_non_superadmin_per_org_get_returns_403(session_factory):
    """Regular OWNER must be rejected with 403 on GET /admin/orgs/{id}/features."""
    ids = await _seed(session_factory)

    async def owner_resolver(factory):
        return await _get_user_by_id(factory, ids["owner_id"])

    app = _make_app(session_factory, owner_resolver)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get(f"/api/v1/admin/orgs/{ids['org_id']}/features")
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_cold_start_inherit_returns_null_and_audits(session_factory):
    """PUT inherit with NO SystemSetting row → 200, global_value null, audit written."""
    ids = await _seed(session_factory)

    async def superadmin_resolver(factory):
        return await _get_user_by_id(factory, ids["superadmin_id"])

    app = _make_app(session_factory, superadmin_resolver)
    client = TestClient(app, raise_server_exceptions=True)

    before = await _count_audit_events(session_factory, "feature.global.set")

    # No prior SystemSetting row exists — cold start
    put_resp = client.put(
        "/api/v1/admin/features/plans",
        json={"value": "inherit"},
    )
    assert put_resp.status_code == 200, put_resp.text
    result = put_resp.json()
    assert result["global_value"] is None

    after = await _count_audit_events(session_factory, "feature.global.set")
    assert after == before + 1, "Expected one new feature.global.set audit row even for no-op inherit"


@pytest.mark.asyncio
async def test_global_put_writes_audit_event(session_factory):
    """Each successful global PUT inserts a feature.global.set audit row with correct payload."""
    ids = await _seed(session_factory)

    async def superadmin_resolver(factory):
        return await _get_user_by_id(factory, ids["superadmin_id"])

    app = _make_app(session_factory, superadmin_resolver)
    client = TestClient(app, raise_server_exceptions=True)

    before = await _count_audit_events(session_factory, "feature.global.set")

    client.put("/api/v1/admin/features/reports", json={"value": "on"})

    after = await _count_audit_events(session_factory, "feature.global.set")
    assert after == before + 1, "Expected one new feature.global.set audit row"

    row = await _latest_audit_event(session_factory, "feature.global.set")
    assert row is not None
    assert row.actor_user_id == ids["superadmin_id"]
    assert row.outcome == "success"
    assert row.target_org_id is None, "Global audit must have no target_org_id"
    assert row.detail["feature"] == "reports"
    assert row.detail["old"] == "inherit"  # no prior SystemSetting row → inherit
    assert row.detail["new"] == "on"


@pytest.mark.asyncio
async def test_per_org_put_writes_audit_event(session_factory):
    """Each successful per-org PUT inserts a feature.org.set audit row with correct payload."""
    ids = await _seed(session_factory)

    async def superadmin_resolver(factory):
        return await _get_user_by_id(factory, ids["superadmin_id"])

    app = _make_app(session_factory, superadmin_resolver)
    client = TestClient(app, raise_server_exceptions=True)

    org_id = ids["org_id"]
    before = await _count_audit_events(session_factory, "feature.org.set")

    client.put(
        f"/api/v1/admin/orgs/{org_id}/features/reports",
        json={"value": "on"},
    )

    after = await _count_audit_events(session_factory, "feature.org.set")
    assert after == before + 1, "Expected one new feature.org.set audit row"

    row = await _latest_audit_event(session_factory, "feature.org.set")
    assert row is not None
    assert row.actor_user_id == ids["superadmin_id"]
    assert row.outcome == "success"
    assert row.target_org_id == org_id, "Per-org audit must carry the target org_id"
    assert row.detail["feature"] == "reports"
    assert row.detail["old"] == "inherit"  # no prior OrgSetting row → inherit
    assert row.detail["new"] == "on"


# ─── Finding A: 403-before-422 ordering ───────────────────────────


@pytest.mark.asyncio
async def test_non_superadmin_invalid_body_returns_403_not_422(session_factory):
    """Authenticated non-superadmin with INVALID body must get 403, not 422.

    Proves that the superadmin check runs as a router-level dependency
    (before body validation) and does not leak schema shape to regular users.
    """
    ids = await _seed(session_factory)

    async def owner_resolver(factory):
        return await _get_user_by_id(factory, ids["owner_id"])

    app = _make_app(session_factory, owner_resolver)
    client = TestClient(app, raise_server_exceptions=True)

    # "garbage" is not a valid Literal["on", "off", "inherit"] — would be 422
    # if body validation ran first; must be 403 because authz runs first.
    resp = client.put(
        "/api/v1/admin/features/reports",
        json={"value": "garbage"},
    )
    assert resp.status_code == 403, (
        f"Expected 403 (authz before body validation), got {resp.status_code}: {resp.text}"
    )


# ─── Finding B: canonical on/off display ──────────────────────────


@pytest.mark.asyncio
async def test_non_canonical_stored_value_shows_canonical_in_get(session_factory):
    """A SystemSetting stored as 'ON' (non-canonical) must be reported as 'on'.

    Proves that the GET display uses normalize_onoff() — the same normalization
    as the gate — so the console and the resolution layer agree.
    """
    ids = await _seed(session_factory)

    # Seed a non-canonical value directly into the DB, bypassing the API
    async with session_factory() as db:
        db.add(SystemSetting(key="feature.plans", value="ON"))
        await db.commit()

    async def superadmin_resolver(factory):
        return await _get_user_by_id(factory, ids["superadmin_id"])

    app = _make_app(session_factory, superadmin_resolver)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get("/api/v1/admin/features")
    assert resp.status_code == 200, resp.text
    plans = next(i for i in resp.json() if i["feature"] == "plans")
    assert plans["global_value"] == "on", (
        f"Expected canonical 'on', got {plans['global_value']!r}"
    )


@pytest.mark.asyncio
async def test_non_canonical_org_override_shows_canonical_in_get(session_factory):
    """An OrgSetting stored as ' on ' (non-canonical) must be reported as 'on'.

    Proves the per-org override display also uses normalize_onoff().
    """
    ids = await _seed(session_factory)
    org_id = ids["org_id"]

    # Seed a non-canonical value directly into the DB, bypassing the API
    async with session_factory() as db:
        db.add(OrgSetting(org_id=org_id, key="feature.reports", value=" on "))
        await db.commit()

    async def superadmin_resolver(factory):
        return await _get_user_by_id(factory, ids["superadmin_id"])

    app = _make_app(session_factory, superadmin_resolver)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get(f"/api/v1/admin/orgs/{org_id}/features")
    assert resp.status_code == 200, resp.text
    reports = next(i for i in resp.json() if i["feature"] == "reports")
    assert reports["override"] == "on", (
        f"Expected canonical 'on', got {reports['override']!r}"
    )
