"""``/refresh`` structured rejection logging, 503-over-401 precedence across
the cookie list, and the 500 for a genuine programmer bug.

Restored from the Redis transport integration file (INFRA-122); only the
store injection changed: ``state_db.session_validate`` now raises a
``sqlalchemy.exc.OperationalError`` where the Redis client used to raise.
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
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app import state_db
from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_session_factory
from app.models import Base
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers import auth as auth_module
from app.routers.auth import router as auth_router
from app.security import decode_refresh_jti_sid, hash_password
from tests.conftest import issue_test_refresh_token, set_refresh_cookie


PASSWORD = "starting-password-1"


class _LogRecorder:
    """Collects structlog events emitted on ``app.routers.auth._LOGGER``.

    ⚠ Deliberately NOT ``structlog.testing.capture_logs()``. That swaps the
    processor chain on the GLOBAL structlog config — which ``app.main``
    already replaced by calling ``setup_logging()`` at import, and which
    other modules in this suite reconfigure without restoring. So whether a
    ``capture_logs`` fence sees anything depends entirely on what ran before
    it: green alone, red in a full or parallel run. Binding onto the
    module's own logger is immune to all of it. Same remedy as
    ``tests/auth/test_anonymous_audit_bounds``.
    """

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def _add(self, level: str, event: str, kw: dict[str, Any]) -> None:
        self.events.append({"event": event, "log_level": level, **kw})

    def debug(self, event: str, **kw: Any) -> None:
        self._add("debug", event, kw)

    def info(self, event: str, **kw: Any) -> None:
        self._add("info", event, kw)

    def warning(self, event: str, **kw: Any) -> None:
        self._add("warning", event, kw)

    def error(self, event: str, **kw: Any) -> None:
        self._add("error", event, kw)

    def bind(self, **_kw: Any) -> "_LogRecorder":
        return self


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


def _fail_validate(monkeypatch, exc, only_jtis=None):
    """Make ``state_db.session_validate`` raise ``exc`` (for ``only_jtis``,
    or for every jti when None); other jtis use the real store."""
    real = state_db.session_validate

    async def _validate(jti):
        if only_jtis is None or jti in only_jtis:
            raise exc
        return await real(jti)

    monkeypatch.setattr(state_db, "session_validate", _validate)


def _store_error():
    return OperationalError("SELECT", {}, Exception("mysql down"))


# ── Structured rejection logging ────────────────────────────────────────


class TestRefreshRejectedLogging:
    """Every terminal 401 path emits one ``auth.refresh.rejected``
    structlog event with a stable ``reason`` enum. Ops uses this to
    distinguish the seven 401 paths without seeing raw refresh tokens.
    ``jti_h`` / ``sid_h`` are 8-char SHA-256 prefixes; raw ``jti``/
    ``sid`` are NEVER logged.

    Observed by binding ``_LogRecorder`` onto ``auth._LOGGER`` (NOT pytest
    ``caplog``, which cannot see native structlog renderers, and NOT
    ``structlog.testing.capture_logs()`` — see ``_LogRecorder``'s docstring
    for why that one is order-dependent).
    """

    @pytest.mark.asyncio
    async def test_invalid_token_logs_reason(
        self, session_factory, monkeypatch
    ) -> None:
        recorder = _LogRecorder()
        monkeypatch.setattr(auth_module, "_LOGGER", recorder)

        app = _make_app(session_factory)
        with TestClient(app) as client:
            set_refresh_cookie(client, "not.a.jwt")
            res = client.post(
                "/api/v1/auth/refresh"
            )
        captured = recorder.events
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
        self, session_factory, monkeypatch
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

        recorder = _LogRecorder()
        monkeypatch.setattr(auth_module, "_LOGGER", recorder)

        app = _make_app(session_factory)
        with TestClient(app) as client:
            set_refresh_cookie(client, legacy_token)
            res = client.post(
                "/api/v1/auth/refresh"
            )
        captured = recorder.events
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
        self, session_factory, monkeypatch
    ) -> None:
        """PII guard: raw jti and sid values must NEVER appear in any
        captured log event. Only the 8-char hash prefix is allowed."""
        from app.security import create_refresh_token

        seed = await _seed_user(session_factory)
        # Hand-mint a token with known jti/sid we can grep for.
        # ``create_refresh_token`` returns ``(token, jti, sid)`` but does
        # NOT insert the session family — so the validation
        # chain hits the "redis_primary_and_grace_missing" path.
        token, jti, sid = create_refresh_token(
            seed["user_id"], ttl_seconds=3600
        )

        recorder = _LogRecorder()
        monkeypatch.setattr(auth_module, "_LOGGER", recorder)

        app = _make_app(session_factory)
        with TestClient(app) as client:
            set_refresh_cookie(client, token)
            res = client.post(
                "/api/v1/auth/refresh"
            )
        captured = recorder.events
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
        self, session_factory, monkeypatch
    ) -> None:
        """Empty cookie header → ``no_refresh_token`` log event. This
        is the diagnostic the 2026-05-19 overnight incident needs:
        when the browser stops sending the refresh cookie, ops can
        distinguish "cookie missing" from "cookie present but
        invalid"."""
        recorder = _LogRecorder()
        monkeypatch.setattr(auth_module, "_LOGGER", recorder)

        app = _make_app(session_factory)
        with TestClient(app) as client:
            res = client.post("/api/v1/auth/refresh")
        captured = recorder.events
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
    order. If the FIRST one hits a store failure (503) and
    the SECOND one is invalid (401), the response MUST be 503, not
    401 — otherwise a transient infra blip on the live cookie would
    force a real logout because the stale cookie's 401 overwrote the
    503 as the final ``last_exc``.

    This is the architect's P1 fix on PR #314: terminal-auth (401)
    must never silently overwrite a transient (5xx) seen earlier
    across the cookie list.
    """

    @pytest.mark.asyncio
    async def test_first_cookie_503_beats_second_cookie_401(
        self, session_factory, monkeypatch
    ) -> None:
        """Two refresh_token cookies in the header. The first hits a
        store error → 503; the second is a
        malformed JWT → 401 before it ever touches the store. Final
        status MUST be 503."""
        seed = await _seed_user(session_factory)
        good_token = issue_test_refresh_token(seed["user_id"])
        app = _make_app(session_factory)

        # Store error only for the first cookie's jti; the second cookie
        # ("not.a.jwt") fails JWT decode before any store call.
        _fail_validate(
            monkeypatch, _store_error(), {decode_refresh_jti_sid(good_token)[0]}
        )
        with TestClient(app) as client:
            res = client.post(
                "/api/v1/auth/refresh",
                headers={
                    # Two cookies, same name, in arrival order: the
                    # valid JWT first (will hit the store → 503), the
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
        self, session_factory, monkeypatch
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
        self, session_factory, monkeypatch
    ) -> None:
        """Belt-and-braces: two cookies, both produce 503. Result is
        still 503 (transient_exc captured from the first; last_exc
        also 5xx)."""
        seed = await _seed_user(session_factory)
        a = issue_test_refresh_token(seed["user_id"])
        b = issue_test_refresh_token(seed["user_id"])
        app = _make_app(session_factory)
        _fail_validate(monkeypatch, _store_error())
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
    inside the rotation outcome branches against the real SQLite state engine."""

    @pytest.mark.asyncio
    async def test_lua_session_revoked_logs_reason(
        self, session_factory, monkeypatch
    ) -> None:
        """When the Lua script returns ``session_revoked`` (concurrent
        /logout deleted the family set), the rotation handler emits
        ``lua_session_revoked`` and raises 401."""
        from app.routers import auth as auth_module
        from app.state_db import SESSION_ROTATE_REVOKED

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

        recorder = _LogRecorder()
        monkeypatch.setattr(auth_module, "_LOGGER", recorder)

        with TestClient(app) as client:
            set_refresh_cookie(client, token)
            res = client.post(
                "/api/v1/auth/refresh"
            )
        captured = recorder.events
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
        from app.state_db import SESSION_ROTATE_ALREADY_ROTATED

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
            state_db, "session_grace", _stub_grace_missing
        )
        monkeypatch.setattr(
            state_db,
            "session_family_exists",
            _stub_family_alive,
        )

        recorder = _LogRecorder()
        monkeypatch.setattr(auth_module, "_LOGGER", recorder)

        with TestClient(app) as client:
            set_refresh_cookie(client, token)
            res = client.post(
                "/api/v1/auth/refresh"
            )
        captured = recorder.events
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


# ── Programmer bugs stay 500 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_returns_500_on_genuine_programmer_bug(
    session_factory, monkeypatch
) -> None:
    """CRITICAL safety property of the narrow filter: a bare
    ``RuntimeError`` (not a ``SQLAlchemyError``) MUST still propagate as
    500. If this test ever passes with status 503, the filter has been
    widened too far and real programmer bugs would be silently swallowed
    as "Service Unavailable" in production."""
    seed = await _seed_user(session_factory)
    token = issue_test_refresh_token(seed["user_id"])
    app = _make_app(session_factory)

    _fail_validate(monkeypatch, RuntimeError("programmer bug: list index out of range"))
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


