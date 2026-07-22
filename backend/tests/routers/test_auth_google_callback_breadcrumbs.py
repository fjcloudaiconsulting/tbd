"""Breadcrumb observability for the Google SSO callback
(spec ``google-callback-observability-2026-05-20.md``).

The 2026-05-19 production incident hung somewhere between the userinfo
fetch and the redirect, but no step emitted a log line, so ops could not
tell which ``await`` was stuck. The callback now emits one gated structlog
breadcrumb per phase (``auth.google.callback.phase``) so the last phase
before silence pins the hung await.

These tests assert:
  - all eight named phases fire, in order, on a successful callback when
    ``AUTH_DEBUG_LOGGING`` is on;
  - nothing is emitted when the flag is off (production stays quiet);
  - the breadcrumbs carry no token / raw-email PII.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_session_factory
from app.models import Base
from app.models.subscription import Plan
from app.rate_limit import limiter
from app.routers import auth as auth_module
from app.routers.auth import router as auth_router

# The eight architect-named phases, in the order the handler runs them
# (spec §1). A successful new-user callback fires all eight.
EXPECTED_PHASES = [
    "userinfo_ok",
    "db_user_lookup_ok",
    "user_prepare_ok",
    "ttl_resolved",
    "session_issue_ok",
    "db_commit_ok",
    "redirect_built",
    "audit_ok",
]

PHASE_EVENT = "auth.google.callback.phase"


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


@pytest.fixture(autouse=True)
def reset_limiter():
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def google_config(monkeypatch):
    monkeypatch.setattr(app_settings, "google_client_id", "test-client-id")
    monkeypatch.setattr(app_settings, "google_client_secret", "test-client-secret")
    monkeypatch.setattr(app_settings, "app_url", "http://localhost")
    yield


def _make_app(session_factory) -> FastAPI:
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_session_factory():
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.include_router(auth_router)
    return app


async def _seed_default_plan(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as db:
        existing = await db.scalar(select(Plan).where(Plan.slug == "free"))
        if existing is None:
            db.add(Plan(slug="free", name="Free", is_active=True, sort_order=0))
            await db.commit()


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict[str, Any]:
        return self._payload


def _patch_httpx(monkeypatch, *, userinfo_email: str) -> None:
    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def post(self, *args: Any, **kwargs: Any) -> _FakeResponse:
            return _FakeResponse(200, {"access_token": "fake-google-token"})

        async def get(self, *args: Any, **kwargs: Any) -> _FakeResponse:
            return _FakeResponse(
                200,
                {
                    "email": userinfo_email,
                    "verified_email": True,
                    "given_name": "First",
                    "family_name": "Last",
                },
            )

    monkeypatch.setattr(auth_module.httpx, "AsyncClient", _FakeClient)


def _phase_calls(logger_mock) -> list[dict]:
    """The kwargs of every ``_LOGGER.info(PHASE_EVENT, ...)`` call, in order."""
    return [
        call.kwargs
        for call in logger_mock.info.call_args_list
        if call.args and call.args[0] == PHASE_EVENT
    ]


def _run_callback(session_factory, *, email: str) -> None:
    app = _make_app(session_factory)
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "matching-state")
        res = client.get(
            "/api/v1/auth/google/callback",
            params={"code": "dummy", "state": "matching-state"},
            follow_redirects=False,
        )
    assert res.status_code == 302, res.text


@pytest.mark.asyncio
async def test_breadcrumbs_fire_in_order_with_debug_on(
    session_factory, google_config, monkeypatch
) -> None:
    await _seed_default_plan(session_factory)
    _patch_httpx(monkeypatch, userinfo_email="brand-new@example.com")
    monkeypatch.setattr(app_settings, "auth_debug_logging", True)

    with patch.object(auth_module, "_LOGGER") as logger_mock:
        _run_callback(session_factory, email="brand-new@example.com")

    calls = _phase_calls(logger_mock)
    assert [c["phase"] for c in calls] == EXPECTED_PHASES
    # Every breadcrumb carries a numeric per-phase duration.
    for c in calls:
        assert isinstance(c["duration_ms"], (int, float))


@pytest.mark.asyncio
async def test_no_breadcrumbs_with_debug_off(
    session_factory, google_config, monkeypatch
) -> None:
    await _seed_default_plan(session_factory)
    _patch_httpx(monkeypatch, userinfo_email="brand-new@example.com")
    monkeypatch.setattr(app_settings, "auth_debug_logging", False)

    with patch.object(auth_module, "_LOGGER") as logger_mock:
        _run_callback(session_factory, email="brand-new@example.com")

    assert _phase_calls(logger_mock) == []


@pytest.mark.asyncio
async def test_breadcrumbs_carry_no_token_or_email_pii(
    session_factory, google_config, monkeypatch
) -> None:
    await _seed_default_plan(session_factory)
    _patch_httpx(monkeypatch, userinfo_email="brand-new@example.com")
    monkeypatch.setattr(app_settings, "auth_debug_logging", True)

    with patch.object(auth_module, "_LOGGER") as logger_mock:
        _run_callback(session_factory, email="brand-new@example.com")

    for c in _phase_calls(logger_mock):
        blob = repr(c).lower()
        assert "brand-new@example.com" not in blob
        assert "fake-google-token" not in blob
        assert "token" not in blob  # no access/refresh token value or key
