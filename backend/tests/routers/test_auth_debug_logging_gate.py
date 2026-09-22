"""``settings.auth_debug_logging`` gate around ``_log_refresh_rejected``.

Production default is OFF — terminal-401 paths still 401, but the
``auth.refresh.rejected`` structured event does NOT emit. Operators
flip the flag ON via the ``AUTH_DEBUG_LOGGING`` env var during incident
triage and OFF again once the diagnosis is in hand.

Two pin points:
  1. Gate OFF → no ``auth.refresh.rejected`` event under any rejection.
  2. Gate ON → events emit as before (existing behaviour preserved).

The 401 itself is NOT gated — only the diagnostic emission is.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_session_factory
from app.models import Base
from app.rate_limit import limiter
from app.routers import auth as auth_module
from app.routers.auth import router as auth_router

from tests.conftest import set_refresh_cookie


class _Recorder:
    """Collects structlog events off ``auth._LOGGER``.

    ⚠ Deliberately NOT ``structlog.testing.capture_logs()``. That swaps the
    processor chain on the GLOBAL structlog config, which several modules in
    this suite reconfigure without restoring, so a capture_logs fence is
    green alone and red in a full or parallel run (``app.main`` calls
    ``setup_logging()`` at import). Worse for the two gate-OFF legs below,
    whose assertion is an EMPTY list: a capture that silently stops
    capturing makes them vacuously green — they would pass against a gate
    that does not gate. Binding the recorder onto the module's own logger
    removes both failure modes. Same remedy as
    ``tests/auth/test_anonymous_audit_bounds``.
    """

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def _record(self, event: str, **kw: Any) -> None:
        self.events.append({"event": event, **kw})

    debug = info = warning = error = critical = _record

    def rejections(self) -> list[dict[str, Any]]:
        return [ev for ev in self.events if ev["event"] == "auth.refresh.rejected"]


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


class TestAuthDebugLoggingGate:
    @pytest.mark.asyncio
    async def test_gate_off_suppresses_event_but_still_401s(
        self, session_factory, monkeypatch
    ) -> None:
        """Production default. A terminal 401 still fires; the
        ``auth.refresh.rejected`` event does NOT emit."""
        # Override the autouse-True from conftest.
        monkeypatch.setattr(app_settings, "auth_debug_logging", False)

        recorder = _Recorder()
        monkeypatch.setattr(auth_module, "_LOGGER", recorder)

        app = _make_app(session_factory)
        with TestClient(app) as client:
            set_refresh_cookie(client, "not.a.jwt")
            res = client.post(
                "/api/v1/auth/refresh"
            )
        assert res.status_code == 401, res.json()
        rejection_logs = recorder.rejections()
        assert rejection_logs == [], (
            f"Gate OFF must suppress auth.refresh.rejected events; got: "
            f"{rejection_logs}"
        )

    @pytest.mark.asyncio
    async def test_gate_off_suppresses_event_on_empty_cookie(
        self, session_factory, monkeypatch
    ) -> None:
        """Same property for the no-cookie path. The 2026-05-19 logs
        rely on this reason being the most useful diagnostic — when
        the gate is off, even that diagnostic stays quiet."""
        monkeypatch.setattr(app_settings, "auth_debug_logging", False)

        recorder = _Recorder()
        monkeypatch.setattr(auth_module, "_LOGGER", recorder)

        app = _make_app(session_factory)
        with TestClient(app) as client:
            res = client.post("/api/v1/auth/refresh")
        assert res.status_code == 401
        assert recorder.rejections() == [], (
            f"Gate OFF must suppress auth.refresh.rejected events; got: "
            f"{recorder.rejections()}"
        )

    @pytest.mark.asyncio
    async def test_gate_on_emits_event(self, session_factory, monkeypatch) -> None:
        """When the operator flips the gate on (the test suite's
        autouse-True default), the event emits with the correct
        reason. Confirms the gate doesn't change anything other than
        emission."""
        assert app_settings.auth_debug_logging is True, (
            "conftest autouse fixture should enable the gate"
        )

        recorder = _Recorder()
        monkeypatch.setattr(auth_module, "_LOGGER", recorder)

        app = _make_app(session_factory)
        with TestClient(app) as client:
            set_refresh_cookie(client, "not.a.jwt")
            res = client.post(
                "/api/v1/auth/refresh"
            )
        assert res.status_code == 401
        rejection_logs = recorder.rejections()
        assert len(rejection_logs) == 1, rejection_logs
        assert rejection_logs[0]["reason"] == "invalid_token_decode"

    def test_settings_default_is_false(self) -> None:
        """Pydantic-settings default must be False so production stays
        quiet without an explicit env var override."""
        # Build a fresh Settings instance without env overrides to
        # confirm the static default is False (the autouse fixture
        # monkeypatches the live instance, but the static default is
        # what production gets without ``AUTH_DEBUG_LOGGING=true``).
        from app.config import Settings

        # Pass a no-op _env_file to bypass any local .env that might
        # set the flag.
        fresh = Settings(_env_file=None)  # type: ignore[call-arg]
        assert fresh.auth_debug_logging is False
