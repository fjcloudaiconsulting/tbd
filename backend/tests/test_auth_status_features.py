"""TDD tests for /api/v1/auth/status resolved feature flags.

Task 4: the endpoint must return a ``features`` object with per-org resolved
feature flags when a valid bearer token is presented, and fall back to
global/env resolution when the caller is unauthenticated.

Coverage:
- Unauthenticated: features.reports / features.plans / features.custom_dashboard
  reflect global/env.
- Authenticated: per-org OrgSetting override is applied.
- Backward-compat: existing keys (needs_setup, captcha_required,
  billing_ui_enabled, feature_reports_v2) must still be present.
- custom_dashboard defaults ON (env-floor now ships True); a per-org OFF
  override rolls it back.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_current_user, get_current_user_optional
from app.models import Base
from app.models.settings import OrgSetting
from app.models.system_setting import SystemSetting
from app.models.user import Organization, Role, User
from app.routers.auth import router as auth_router
from app.security import hash_password
from app.services.feature_gate import Feature, feature_setting_key


# ---------------------------------------------------------------------------
# Fixtures
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


def _make_unauthed_app(session_factory) -> FastAPI:
    """App with no user override — credentials are absent → optional dep returns None."""
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.include_router(auth_router)
    return app


def _make_authed_app(session_factory, user: User) -> FastAPI:
    """App whose get_current_user_optional always returns the supplied user."""
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_optional_user() -> User:
        return user

    # Override both — get_current_user is imported by other auth routes.
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_optional] = override_optional_user
    app.dependency_overrides[get_current_user] = override_optional_user
    app.include_router(auth_router)
    return app


# ---------------------------------------------------------------------------
# Unauthenticated — global/env resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unauthenticated_features_reflect_env_floor(
    session_factory, monkeypatch
) -> None:
    """Without a token the endpoint resolves features from env only (no DB rows).

    feature_reports_v2=True, feature_plans=False → reports True, plans False.
    custom_dashboard env-floor pinned False here → off.
    """
    monkeypatch.setattr(app_settings, "feature_reports_v2", True)
    monkeypatch.setattr(app_settings, "feature_plans", False)
    monkeypatch.setattr(app_settings, "feature_custom_dashboard", False)
    monkeypatch.setattr(app_settings, "captcha_required", False)
    monkeypatch.setattr(app_settings, "billing_ui_enabled", False)

    app = _make_unauthed_app(session_factory)
    with TestClient(app) as client:
        resp = client.get("/api/v1/auth/status")

    assert resp.status_code == 200
    body = resp.json()
    assert "features" in body
    assert body["features"]["reports"] is True
    assert body["features"]["plans"] is False
    assert body["features"]["custom_dashboard"] is False


@pytest.mark.asyncio
async def test_unauthenticated_features_env_floor_inverted(
    session_factory, monkeypatch
) -> None:
    """Inverted env: reports off, plans on (via GlobalSystemSetting only)."""
    monkeypatch.setattr(app_settings, "feature_reports_v2", False)
    monkeypatch.setattr(app_settings, "feature_plans", False)
    monkeypatch.setattr(app_settings, "captcha_required", False)
    monkeypatch.setattr(app_settings, "billing_ui_enabled", False)

    # Put a global SystemSetting to turn plans on at global level
    async with session_factory() as db:
        db.add(SystemSetting(key=feature_setting_key(Feature.PLANS), value="on"))
        await db.commit()

    app = _make_unauthed_app(session_factory)
    with TestClient(app) as client:
        resp = client.get("/api/v1/auth/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["features"]["reports"] is False
    assert body["features"]["plans"] is True


@pytest.mark.asyncio
async def test_unauthenticated_backward_compat_keys_present(
    session_factory, monkeypatch
) -> None:
    """Existing keys must still be present after adding the features block."""
    monkeypatch.setattr(app_settings, "feature_reports_v2", False)
    monkeypatch.setattr(app_settings, "feature_plans", False)
    monkeypatch.setattr(app_settings, "captcha_required", True)
    monkeypatch.setattr(app_settings, "billing_ui_enabled", False)

    app = _make_unauthed_app(session_factory)
    with TestClient(app) as client:
        resp = client.get("/api/v1/auth/status")

    assert resp.status_code == 200
    body = resp.json()
    for key in ("needs_setup", "captcha_required", "billing_ui_enabled", "feature_reports_v2"):
        assert key in body, f"backward-compat key missing: {key}"
    assert body["captcha_required"] is True


# ---------------------------------------------------------------------------
# Authenticated — per-org OrgSetting override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authenticated_org_override_plans(session_factory, monkeypatch) -> None:
    """When org has OrgSetting feature.plans=on (global/env off) → plans True."""
    monkeypatch.setattr(app_settings, "feature_plans", False)
    monkeypatch.setattr(app_settings, "feature_reports_v2", False)
    monkeypatch.setattr(app_settings, "captcha_required", False)
    monkeypatch.setattr(app_settings, "billing_ui_enabled", False)

    async with session_factory() as db:
        org = Organization(name="Test Org", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id,
            username="owner1",
            email="owner1@example.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            email_verified=True,
        )
        db.add(user)
        await db.flush()
        # Per-org: plans=on; global/env: plans=off
        db.add(OrgSetting(org_id=org.id, key=feature_setting_key(Feature.PLANS), value="on"))
        await db.commit()
        await db.refresh(user)

    app = _make_authed_app(session_factory, user)
    with TestClient(app) as client:
        resp = client.get("/api/v1/auth/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["features"]["plans"] is True
    assert body["features"]["reports"] is False  # no override, env off


@pytest.mark.asyncio
async def test_authenticated_org_override_reports(session_factory, monkeypatch) -> None:
    """When org has OrgSetting feature.reports=off (env on) → reports False."""
    monkeypatch.setattr(app_settings, "feature_reports_v2", True)
    monkeypatch.setattr(app_settings, "feature_plans", False)
    monkeypatch.setattr(app_settings, "captcha_required", False)
    monkeypatch.setattr(app_settings, "billing_ui_enabled", False)

    async with session_factory() as db:
        org = Organization(name="Org B", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id,
            username="owner2",
            email="owner2@example.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            email_verified=True,
        )
        db.add(user)
        await db.flush()
        # Per-org override disables reports even though env says on
        db.add(OrgSetting(org_id=org.id, key=feature_setting_key(Feature.REPORTS), value="off"))
        await db.commit()
        await db.refresh(user)

    app = _make_authed_app(session_factory, user)
    with TestClient(app) as client:
        resp = client.get("/api/v1/auth/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["features"]["reports"] is False
    assert body["features"]["plans"] is False


@pytest.mark.asyncio
async def test_authenticated_no_org_override_uses_env(session_factory, monkeypatch) -> None:
    """Authenticated user with no OrgSetting rows → env floor applies."""
    monkeypatch.setattr(app_settings, "feature_reports_v2", True)
    monkeypatch.setattr(app_settings, "feature_plans", True)
    monkeypatch.setattr(app_settings, "captcha_required", False)
    monkeypatch.setattr(app_settings, "billing_ui_enabled", False)

    async with session_factory() as db:
        org = Organization(name="Org C", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id,
            username="owner3",
            email="owner3@example.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            email_verified=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    app = _make_authed_app(session_factory, user)
    with TestClient(app) as client:
        resp = client.get("/api/v1/auth/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["features"]["reports"] is True
    assert body["features"]["plans"] is True


# ---------------------------------------------------------------------------
# HTTP-level fail-closed: bad token → optional dep returns None, not 401/500
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bad_token_treated_as_unauthenticated(session_factory, monkeypatch) -> None:
    """A well-formed but invalid Bearer token must NOT raise 401 or 500.

    get_current_user_optional runs for real (no override); it sees the bad JWT,
    returns None, and the endpoint resolves features from env/global — exactly
    the same as an unauthenticated call.  Proves the optional-auth path fails
    closed at the HTTP layer.
    """
    monkeypatch.setattr(app_settings, "feature_reports_v2", True)
    monkeypatch.setattr(app_settings, "feature_plans", False)
    monkeypatch.setattr(app_settings, "captcha_required", False)
    monkeypatch.setattr(app_settings, "billing_ui_enabled", False)

    # Build the app WITHOUT overriding get_current_user_optional so the real
    # dependency runs.  Only override get_db (and get_session_factory so any
    # independent-session code also uses the in-memory DB).
    from app.deps import get_session_factory

    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_get_session_factory() -> async_sessionmaker[AsyncSession]:
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = override_get_session_factory
    app.include_router(auth_router)

    with TestClient(app) as client:
        resp = client.get(
            "/api/v1/auth/status",
            headers={"Authorization": "Bearer not-a-real-token"},
        )

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert "features" in body
    # Bad token → treated as no user → env-floor resolution
    assert body["features"]["reports"] is True
    assert body["features"]["plans"] is False


# ---------------------------------------------------------------------------
# custom_dashboard — default ON + org-override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_custom_dashboard_defaults_on_in_status(
    session_factory, monkeypatch
) -> None:
    """features.custom_dashboard is True by default now that the env-floor ships
    True (global flip) and no DB overrides exist. feature_custom_dashboard is
    left un-patched so this asserts the real shipped default."""
    monkeypatch.setattr(app_settings, "feature_reports_v2", False)
    monkeypatch.setattr(app_settings, "feature_plans", False)
    monkeypatch.setattr(app_settings, "captcha_required", False)
    monkeypatch.setattr(app_settings, "billing_ui_enabled", False)

    assert app_settings.feature_custom_dashboard is True

    app = _make_unauthed_app(session_factory)
    with TestClient(app) as client:
        resp = client.get("/api/v1/auth/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["features"]["custom_dashboard"] is True


@pytest.mark.asyncio
async def test_custom_dashboard_flips_with_org_override_in_status(
    session_factory, monkeypatch
) -> None:
    """Per-org OrgSetting 'on' turns custom_dashboard on for that org."""
    monkeypatch.setattr(app_settings, "feature_reports_v2", False)
    monkeypatch.setattr(app_settings, "feature_plans", False)
    monkeypatch.setattr(app_settings, "feature_custom_dashboard", False)
    monkeypatch.setattr(app_settings, "captcha_required", False)
    monkeypatch.setattr(app_settings, "billing_ui_enabled", False)

    async with session_factory() as db:
        org = Organization(name="Dashboard Org", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id,
            username="dash_owner",
            email="dash_owner@example.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            email_verified=True,
        )
        db.add(user)
        await db.flush()
        db.add(
            OrgSetting(
                org_id=org.id,
                key=feature_setting_key(Feature.CUSTOM_DASHBOARD),
                value="on",
            )
        )
        await db.commit()
        await db.refresh(user)

    app = _make_authed_app(session_factory, user)
    with TestClient(app) as client:
        resp = client.get("/api/v1/auth/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["features"]["custom_dashboard"] is True
    # Other features still reflect env (off).
    assert body["features"]["reports"] is False
    assert body["features"]["plans"] is False
