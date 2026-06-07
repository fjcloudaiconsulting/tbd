"""End-to-end coverage for the Redis transport-normalizer fix.

Tests in ``test_redis_transport_normalizer.py`` pin the decorator's
contract in isolation. These tests pin the integrated behaviour: when
``redis_client.session_validate`` raises the closed-transport
``RuntimeError`` from inside FastAPI's request-handling stack, the
router returns **503**, not **500** — the canonical fix for the
2026-05-19T07:10:52 production trace.

Also covered: the structured ``auth.refresh.rejected`` log event fires
on every terminal 401 path with the correct ``reason`` enum.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
import structlog
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
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers.auth import router as auth_router
from app.security import hash_password
from tests.conftest import issue_test_refresh_token, set_refresh_cookie


PASSWORD = "starting-password-1"


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


async def _seed_user(factory) -> dict[str, Any]:
    async with factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id,
            username="alice",
            email="alice@example.com",
            password_hash=hash_password(PASSWORD),
            role=Role.OWNER,
            is_superadmin=False,
            is_active=True,
            email_verified=True,
        )
        db.add(user)
        await db.commit()
        return {"org_id": org.id, "user_id": user.id}


# ── The canonical 2026-05-19T07:10 production trace, end-to-end ─────────


class TestRefreshReturns503OnClosedTransport:
    """When the underlying Redis client raises a closed-transport
    ``RuntimeError``, the ``_normalize_transport_errors`` wrapper
    translates it to ``RedisConnectionError`` so the router's existing
    ``except (RedisRequired, RedisError)`` handler catches it and
    returns **503** — not 500.

    The MOCK strategy: patch the inner Redis client's ``get`` method
    so the wrapped ``session_validate`` function actually runs (and
    the decorator's try/except fires). Patching ``session_validate``
    directly would bypass the wrapper entirely — the test would tell
    us nothing about the integrated behaviour.
    """

    def _patch_client_get(self, side_effect):
        """Context manager: patch the inner Redis client's ``.get``
        method on the autouse fake-Redis instance that's already
        installed by tests/conftest.py. Returns the patch object."""
        # The fake Redis client lives in app.redis_client._client after
        # the autouse fixture runs. Patch its .get to raise the desired
        # exception when called.
        import app.redis_client as rc

        # Force the fake client to materialize if it hasn't.
        client = rc.get_client()
        if client is None:
            pytest.skip(
                "Autouse fake-Redis fixture didn't install a client"
            )
        return patch.object(client, "get", side_effect=side_effect)

    @pytest.mark.asyncio
    async def test_refresh_returns_503_on_closed_transport_runtime_error(
        self, session_factory
    ) -> None:
        seed = await _seed_user(session_factory)
        token = issue_test_refresh_token(seed["user_id"])
        app = _make_app(session_factory)

        # The exact production trace from 2026-05-19T07:10:52.
        closed_transport_error = RuntimeError(
            "unable to perform operation on <TCPTransport closed=True "
            "reading=False 0x55a57d2583e0>; the handler is closed"
        )

        with self._patch_client_get(side_effect=closed_transport_error):
            with TestClient(app) as client:
                set_refresh_cookie(client, token)
                res = client.post(
                    "/api/v1/auth/refresh"
                )

        # The contract: 503, not 500. The frontend reactive-recovery
        # path then treats this as transient and retries.
        assert res.status_code == 503, (
            f"Expected 503, got {res.status_code}: {res.json()}"
        )
        body = res.json()
        # User-facing constant, not the raw transport message.
        assert "temporarily unavailable" in body["detail"].lower()

    @pytest.mark.asyncio
    async def test_refresh_returns_503_on_broken_pipe(
        self, session_factory
    ) -> None:
        """Same property for the OSError class. ``BrokenPipeError``
        derives from ``OSError``; the wrapper catches the whole
        family."""
        seed = await _seed_user(session_factory)
        token = issue_test_refresh_token(seed["user_id"])
        app = _make_app(session_factory)

        with self._patch_client_get(
            side_effect=BrokenPipeError(32, "Broken pipe")
        ):
            with TestClient(app) as client:
                set_refresh_cookie(client, token)
                res = client.post(
                    "/api/v1/auth/refresh"
                )
        assert res.status_code == 503

    @pytest.mark.asyncio
    async def test_refresh_returns_500_on_genuine_programmer_bug(
        self, session_factory
    ) -> None:
        """CRITICAL safety property of the narrow filter: a bare
        ``RuntimeError`` whose message doesn't match a transport
        marker MUST still propagate as 500. If this test ever passes
        with status 503, the filter has been widened too far and
        real programmer bugs would be silently swallowed as
        "Service Unavailable" in production."""
        seed = await _seed_user(session_factory)
        token = issue_test_refresh_token(seed["user_id"])
        app = _make_app(session_factory)

        with self._patch_client_get(
            side_effect=RuntimeError("programmer bug: list index out of range")
        ):
            # raise_server_exceptions=False so TestClient returns the
            # 500 response instead of re-raising the inner exception —
            # we want to assert on the response, not catch the bug.
            with TestClient(app, raise_server_exceptions=False) as client:
                set_refresh_cookie(client, token)
                res = client.post(
                    "/api/v1/auth/refresh"
                )
        assert res.status_code == 500, (
            f"Genuine RuntimeError must stay a 500; got {res.status_code}"
        )


# ── Structured rejection logging ────────────────────────────────────────


class TestRefreshRejectedLogging:
    """Every terminal 401 path emits one ``auth.refresh.rejected``
    structlog event with a stable ``reason`` enum. Ops uses this to
    distinguish the seven 401 paths without seeing raw refresh tokens.
    ``jti_h`` / ``sid_h`` are 8-char SHA-256 prefixes; raw ``jti``/
    ``sid`` are NEVER logged.

    Uses ``structlog.testing.capture_logs()`` (NOT pytest ``caplog``)
    because the app's structlog setup uses native structlog renderers
    rather than the stdlib bridge, so ``caplog.records`` doesn't see
    our events.
    """

    @pytest.mark.asyncio
    async def test_invalid_token_logs_reason(
        self, session_factory
    ) -> None:
        app = _make_app(session_factory)
        with structlog.testing.capture_logs() as captured:
            with TestClient(app) as client:
                set_refresh_cookie(client, "not.a.jwt")
                res = client.post(
                    "/api/v1/auth/refresh"
                )
        assert res.status_code == 401
        rejection_logs = [
            ev for ev in captured if ev.get("event") == "auth.refresh.rejected"
        ]
        assert len(rejection_logs) >= 1, (
            f"Expected auth.refresh.rejected event; got: {captured}"
        )
        assert rejection_logs[0]["reason"] == "invalid_token_decode"

    @pytest.mark.asyncio
    async def test_missing_jti_sid_logs_reason(
        self, session_factory
    ) -> None:
        """A refresh JWT without ``jti``/``sid`` (legacy from before
        PR #306) logs ``missing_jti_or_sid``."""
        from app.security import create_refresh_token

        seed = await _seed_user(session_factory)
        # Build a refresh JWT that LACKS jti/sid — simulate a legacy
        # pre-PR-306 token by stripping those claims after issue.
        import jwt as _jwt
        token = create_refresh_token(seed["user_id"], ttl_seconds=3600)[0]
        payload = _jwt.decode(
            token, app_settings.jwt_secret_key,
            algorithms=[app_settings.jwt_algorithm],
        )
        payload.pop("jti", None)
        payload.pop("sid", None)
        legacy_token = _jwt.encode(
            payload, app_settings.jwt_secret_key,
            algorithm=app_settings.jwt_algorithm,
        )

        app = _make_app(session_factory)
        with structlog.testing.capture_logs() as captured:
            with TestClient(app) as client:
                set_refresh_cookie(client, legacy_token)
                res = client.post(
                    "/api/v1/auth/refresh"
                )
        assert res.status_code == 401
        rejection_logs = [
            ev for ev in captured
            if ev.get("event") == "auth.refresh.rejected"
            and ev.get("reason") == "missing_jti_or_sid"
        ]
        assert len(rejection_logs) >= 1, (
            f"Expected missing_jti_or_sid event; got: {captured}"
        )
        # Confirm the user id field is populated for ops correlation.
        assert rejection_logs[0]["sub"] == seed["user_id"]

    @pytest.mark.asyncio
    async def test_log_event_never_contains_raw_jti_or_sid(
        self, session_factory
    ) -> None:
        """PII guard: raw jti and sid values must NEVER appear in any
        captured log event. Only the 8-char hash prefix is allowed."""
        from app.security import create_refresh_token

        seed = await _seed_user(session_factory)
        # Hand-mint a token with known jti/sid we can grep for.
        # ``create_refresh_token`` returns ``(token, jti, sid)`` but does
        # NOT insert the primary key into Redis — so the validation
        # chain hits the "redis_primary_and_grace_missing" path.
        token, jti, sid = create_refresh_token(
            seed["user_id"], ttl_seconds=3600
        )

        app = _make_app(session_factory)
        with structlog.testing.capture_logs() as captured:
            with TestClient(app) as client:
                set_refresh_cookie(client, token)
                res = client.post(
                    "/api/v1/auth/refresh"
                )
        assert res.status_code == 401

        # Confirm we hit a redacted-log path.
        rejection_logs = [
            ev for ev in captured
            if ev.get("event") == "auth.refresh.rejected"
        ]
        assert rejection_logs, f"No rejection log captured: {captured}"

        # Flatten every captured event to a string and assert that NO
        # field contains the raw jti or sid. Only the hash prefix is
        # acceptable.
        for ev in captured:
            for key, value in ev.items():
                if not isinstance(value, str):
                    continue
                assert value != jti, (
                    f"Raw jti leaked in event field {key!r}: {value!r}"
                )
                assert value != sid, (
                    f"Raw sid leaked in event field {key!r}: {value!r}"
                )

        # The rejection log MUST carry the hash prefix instead.
        assert rejection_logs[0]["jti_h"] is not None
        assert rejection_logs[0]["sid_h"] is not None
        # Hash is 8 hex chars.
        assert len(rejection_logs[0]["jti_h"]) == 8
        assert len(rejection_logs[0]["sid_h"]) == 8

    @pytest.mark.asyncio
    async def test_no_refresh_token_logs_reason(
        self, session_factory
    ) -> None:
        """Empty cookie header → ``no_refresh_token`` log event. This
        is the diagnostic the 2026-05-19 overnight incident needs:
        when the browser stops sending the refresh cookie, ops can
        distinguish "cookie missing" from "cookie present but
        invalid"."""
        app = _make_app(session_factory)
        with structlog.testing.capture_logs() as captured:
            with TestClient(app) as client:
                res = client.post("/api/v1/auth/refresh")
        assert res.status_code == 401
        rejection_logs = [
            ev for ev in captured
            if ev.get("event") == "auth.refresh.rejected"
            and ev.get("reason") == "no_refresh_token"
        ]
        assert len(rejection_logs) == 1, (
            f"Expected exactly one no_refresh_token event; got: {captured}"
        )


# ── 503 wins over a later 401 across the cookie list ────────────────────


class TestRefreshPrefersTransientOverTerminal:
    """When the browser sends BOTH a legacy and a current
    ``refresh_token`` cookie, the validator walks them in arrival
    order. If the FIRST one hits a Redis transport failure (503) and
    the SECOND one is invalid (401), the response MUST be 503, not
    401 — otherwise a transient infra blip on the live cookie would
    force a real logout because the stale cookie's 401 overwrote the
    503 as the final ``last_exc``.

    This is the architect's P1 fix on PR #314: terminal-auth (401)
    must never silently overwrite a transient (5xx) seen earlier
    across the cookie list.
    """

    def _patch_client_get(self, side_effect):
        """Same client-level patch trick as the parent file: patch
        the inner Redis client's ``.get`` so the wrapper actually
        runs. ``side_effect`` may be a callable for per-call values."""
        import app.redis_client as rc

        client = rc.get_client()
        if client is None:
            pytest.skip(
                "Autouse fake-Redis fixture didn't install a client"
            )
        return patch.object(client, "get", side_effect=side_effect)

    @pytest.mark.asyncio
    async def test_first_cookie_503_beats_second_cookie_401(
        self, session_factory
    ) -> None:
        """Two refresh_token cookies in the header. The first hits a
        closed-transport RuntimeError → 503; the second is a
        malformed JWT → 401 before it ever touches Redis. Final
        status MUST be 503."""
        seed = await _seed_user(session_factory)
        good_token = issue_test_refresh_token(seed["user_id"])
        app = _make_app(session_factory)

        # Closed-transport RuntimeError only on the first .get call;
        # the second cookie ("not.a.jwt") fails JWT decode before
        # any Redis call, so .get is never called for it.
        with self._patch_client_get(
            side_effect=RuntimeError(
                "unable to perform operation on <TCPTransport closed=True "
                "reading=False 0x0>; the handler is closed"
            ),
        ):
            with TestClient(app) as client:
                res = client.post(
                    "/api/v1/auth/refresh",
                    headers={
                        # Two cookies, same name, in arrival order: the
                        # valid JWT first (will hit Redis → 503), the
                        # malformed one second (would 401 on decode).
                        "cookie": (
                            f"refresh_token={good_token}; "
                            f"refresh_token=not.a.jwt"
                        ),
                    },
                )

        # The contract: 503 wins. A 401 here would be the regression.
        assert res.status_code == 503, (
            f"Expected 503 (transient), got {res.status_code}: "
            f"{res.json()}"
        )

    @pytest.mark.asyncio
    async def test_single_invalid_cookie_still_401(
        self, session_factory
    ) -> None:
        """Sanity guard: the transient-preferral logic must NOT
        upgrade a single-cookie 401 to a 503. When only one cookie is
        present and it fails terminally, the response is still 401 —
        no transient ever seen."""
        app = _make_app(session_factory)
        with TestClient(app) as client:
            set_refresh_cookie(client, "not.a.jwt")
            res = client.post(
                "/api/v1/auth/refresh"
            )
        assert res.status_code == 401

    @pytest.mark.asyncio
    async def test_503_first_then_503_returns_503(
        self, session_factory
    ) -> None:
        """Belt-and-braces: two cookies, both produce 503. Result is
        still 503 (transient_exc captured from the first; last_exc
        also 5xx)."""
        seed = await _seed_user(session_factory)
        a = issue_test_refresh_token(seed["user_id"])
        b = issue_test_refresh_token(seed["user_id"])
        app = _make_app(session_factory)
        with self._patch_client_get(
            side_effect=RuntimeError("the handler is closed"),
        ):
            with TestClient(app) as client:
                res = client.post(
                    "/api/v1/auth/refresh",
                    headers={
                        "cookie": f"refresh_token={a}; refresh_token={b}"
                    },
                )
        assert res.status_code == 503


# ── Lua rotation rejection paths emit reason logs ───────────────────────


class TestRefreshLuaRotationLogging:
    """The two rotation-layer terminal 401 paths must emit
    ``auth.refresh.rejected`` events with a stable ``reason`` so ops
    can distinguish them from the earlier validation-chain 401s.

    Architect P2 on PR #314: 'all terminal 401 paths are logged' is the
    contract these tests pin. Tests stub ``_rotate_refresh_session`` at
    the auth-module level so the validation chain succeeds and we land
    inside the rotation outcome branches without spinning up a real
    Lua-capable Redis."""

    @pytest.mark.asyncio
    async def test_lua_session_revoked_logs_reason(
        self, session_factory, monkeypatch
    ) -> None:
        """When the Lua script returns ``session_revoked`` (concurrent
        /logout deleted the family set), the rotation handler emits
        ``lua_session_revoked`` and raises 401."""
        from app.routers import auth as auth_module
        from app.redis_client import SESSION_ROTATE_REVOKED

        seed = await _seed_user(session_factory)
        token = issue_test_refresh_token(seed["user_id"])
        app = _make_app(session_factory)

        async def _stub_rotate(
            user_id, old_jti, sid, *, ttl_seconds, session_created_at,
        ):
            # Return signature: (new_token, new_jti, sid, lua_result)
            return ("unused", "new-jti", sid, SESSION_ROTATE_REVOKED)

        monkeypatch.setattr(
            auth_module, "_rotate_refresh_session", _stub_rotate
        )

        with structlog.testing.capture_logs() as captured:
            with TestClient(app) as client:
                set_refresh_cookie(client, token)
                res = client.post(
                    "/api/v1/auth/refresh"
                )
        assert res.status_code == 401
        assert "invalidated" in res.json()["detail"].lower()

        rejection_logs = [
            ev for ev in captured
            if ev.get("event") == "auth.refresh.rejected"
            and ev.get("reason") == "lua_session_revoked"
        ]
        assert len(rejection_logs) == 1, (
            f"Expected exactly one lua_session_revoked event; got: "
            f"{captured}"
        )
        # PII guard: only hash prefixes, no raw jti/sid.
        assert rejection_logs[0]["sub"] == seed["user_id"]
        assert len(rejection_logs[0]["jti_h"]) == 8
        assert len(rejection_logs[0]["sid_h"]) == 8

    @pytest.mark.asyncio
    async def test_already_rotated_grace_revalidation_failed_logs_reason(
        self, session_factory, monkeypatch
    ) -> None:
        """When the Lua script returns ``already_rotated`` but the
        winner's grace key is gone by the time we re-probe (TTL expired
        or concurrent logout), emit
        ``already_rotated_grace_revalidation_failed`` and 401."""
        from app.routers import auth as auth_module
        from app.redis_client import SESSION_ROTATE_ALREADY_ROTATED

        seed = await _seed_user(session_factory)
        token = issue_test_refresh_token(seed["user_id"])
        app = _make_app(session_factory)

        async def _stub_rotate(
            user_id, old_jti, sid, *, ttl_seconds, session_created_at,
        ):
            return ("unused", "new-jti", sid, SESSION_ROTATE_ALREADY_ROTATED)

        async def _stub_grace_missing(jti):
            return None  # Winner's grace key is gone.

        async def _stub_family_alive(sid):
            return True

        monkeypatch.setattr(
            auth_module, "_rotate_refresh_session", _stub_rotate
        )
        monkeypatch.setattr(
            auth_module.redis_client, "session_grace", _stub_grace_missing
        )
        monkeypatch.setattr(
            auth_module.redis_client,
            "session_family_exists",
            _stub_family_alive,
        )

        with structlog.testing.capture_logs() as captured:
            with TestClient(app) as client:
                set_refresh_cookie(client, token)
                res = client.post(
                    "/api/v1/auth/refresh"
                )
        assert res.status_code == 401

        rejection_logs = [
            ev for ev in captured
            if ev.get("event") == "auth.refresh.rejected"
            and ev.get("reason") == "already_rotated_grace_revalidation_failed"
        ]
        assert len(rejection_logs) == 1, (
            f"Expected exactly one already_rotated_grace_revalidation_failed "
            f"event; got: {captured}"
        )
        # The diagnostic fields let ops triage which check failed.
        ev = rejection_logs[0]
        assert ev["grace_row_missing"] is True
        assert ev["family_alive"] is True
        assert ev["sub"] == seed["user_id"]
