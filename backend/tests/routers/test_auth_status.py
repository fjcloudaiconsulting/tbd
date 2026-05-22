"""Coverage for /api/v1/auth/status — the boot-time signals endpoint.

The frontend reads this on mount and uses the payload to decide:

* ``needs_setup`` — first-run admin bootstrap path.
* ``captcha_required`` — render the Turnstile widget on /register.
* ``billing_ui_enabled`` — render the customer-facing plan / trial /
  billing surface (trial banner, settings Billing tab, /settings/billing
  plan grid).

These tests pin the JSON shape: each flag must be present in the
response and reflect the corresponding ``Settings`` value. Backend flips
must propagate on the next page load, so the test covers both default
and monkeypatched-true paths for ``billing_ui_enabled``.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.database import get_db
from app.models import Base
from app.routers.auth import router as auth_router


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _make_app(session_factory) -> FastAPI:
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.include_router(auth_router)
    return app


@pytest.mark.asyncio
async def test_status_exposes_billing_ui_enabled_default_false(
    session_factory, monkeypatch
) -> None:
    """Pre-payment default: billing_ui_enabled=false in the response."""
    monkeypatch.setattr(app_settings, "billing_ui_enabled", False)
    monkeypatch.setattr(app_settings, "captcha_required", False)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        resp = client.get("/api/v1/auth/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["needs_setup"] is True  # empty DB
    assert body["captcha_required"] is False
    assert body["billing_ui_enabled"] is False


@pytest.mark.asyncio
async def test_status_exposes_billing_ui_enabled_true_when_flag_on(
    session_factory, monkeypatch
) -> None:
    """When the payment platform is wired and the operator flips the
    flag to true, the same endpoint must surface it so the next page
    load restores the customer-facing billing surface.
    """
    monkeypatch.setattr(app_settings, "billing_ui_enabled", True)
    monkeypatch.setattr(app_settings, "captcha_required", False)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        resp = client.get("/api/v1/auth/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["billing_ui_enabled"] is True


@pytest.mark.asyncio
async def test_status_billing_flag_is_independent_of_captcha_flag(
    session_factory, monkeypatch
) -> None:
    """Defense against future regression — the two flags are
    independent control-plane signals and must not share state.
    """
    monkeypatch.setattr(app_settings, "billing_ui_enabled", False)
    monkeypatch.setattr(app_settings, "captcha_required", True)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        resp = client.get("/api/v1/auth/status")

    body = resp.json()
    assert body["captcha_required"] is True
    assert body["billing_ui_enabled"] is False


@pytest.mark.asyncio
async def test_status_exposes_feature_reports_v2_default_false(
    session_factory, monkeypatch
) -> None:
    """Pre-launch default: feature_reports_v2=false in the response.

    The Reports v2 nav item + ``/reports/*`` frontend routes consume
    this signal. Default false until the canvas + widget catalog ship.
    """
    monkeypatch.setattr(app_settings, "feature_reports_v2", False)
    monkeypatch.setattr(app_settings, "billing_ui_enabled", False)
    monkeypatch.setattr(app_settings, "captcha_required", False)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        resp = client.get("/api/v1/auth/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["feature_reports_v2"] is False


@pytest.mark.asyncio
async def test_status_exposes_feature_reports_v2_true_when_flag_on(
    session_factory, monkeypatch
) -> None:
    """When the operator flips ``FEATURE_REPORTS_V2`` to true, the
    same endpoint must surface it so the next page load shows the
    Reports nav item + lights up the ``/reports`` routes.
    """
    monkeypatch.setattr(app_settings, "feature_reports_v2", True)
    monkeypatch.setattr(app_settings, "billing_ui_enabled", False)
    monkeypatch.setattr(app_settings, "captcha_required", False)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        resp = client.get("/api/v1/auth/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["feature_reports_v2"] is True


@pytest.mark.asyncio
async def test_status_reports_flag_is_independent_of_other_flags(
    session_factory, monkeypatch
) -> None:
    """Defense against regression — feature_reports_v2 is an
    independent control-plane signal, not coupled to captcha /
    billing.
    """
    monkeypatch.setattr(app_settings, "feature_reports_v2", True)
    monkeypatch.setattr(app_settings, "billing_ui_enabled", False)
    monkeypatch.setattr(app_settings, "captcha_required", False)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        resp = client.get("/api/v1/auth/status")

    body = resp.json()
    assert body["feature_reports_v2"] is True
    assert body["billing_ui_enabled"] is False
    assert body["captcha_required"] is False
