"""SSO step-up `return_to` allowlist + state shape coverage.

Pins the invariants flagged in the PR #149 review:

  - No `return_to` in the request body encodes the default key into
    state, and the callback redirects to `/settings`.
  - `return_to: "security"` encodes the security key, and the
    callback redirects to `/settings/security#stepup_token=<token>`
    (the issued token, in the URL fragment).
  - An unknown `return_to` value (junk strings, traversal payloads,
    open-redirect-style URLs) MUST NOT redirect to that target. The
    initiate handler silently coerces the key to the default before
    encoding state, so the callback redirects to `/settings`.
  - Malformed state at the callback (3-part legacy shape, empty
    string, junk) returns 400 "Malformed step-up state". No redirect,
    no step-up token issued.

The flow exchanges a Google OAuth code at the callback. We patch the
module-level `httpx.AsyncClient` so the success-path test never
touches the network and so we can assert the redirect URL the router
actually builds.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from sqlalchemy import select

from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.user import Organization, Role, User
from app.routers import auth as auth_module
from app.routers.auth import router as auth_router
from app.security import hash_password


@pytest_asyncio.fixture
async def session_factory():
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


@pytest.fixture
def google_config(monkeypatch):
    """Fill in the Google OAuth knobs so `_validate_google_config` passes
    and the callback's redirect URL has a stable origin to assert on."""
    monkeypatch.setattr(app_settings, "google_client_id", "test-client-id")
    monkeypatch.setattr(app_settings, "google_client_secret", "test-client-secret")
    monkeypatch.setattr(app_settings, "app_url", "http://localhost")
    yield


async def _seed_user(session_factory, *, email: str = "alice@acme.io") -> int:
    async with session_factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        user = User(
            org_id=org.id,
            username="alice",
            email=email,
            password_hash=hash_password("starting-password"),
            role=Role.OWNER,
            is_active=True,
            email_verified=True,
            password_set=True,
        )
        db.add(user)
        await db.commit()
        return user.id


def _make_app(session_factory, current_user_id: int | None):
    """Build a tiny FastAPI app with `get_db` overridden against the
    in-memory SQLite session factory and `get_current_user` resolved
    to the seeded user (when one is supplied)."""
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_session_factory():
        # Audit writes use this independent factory so failure rows
        # commit in their own txn even when the business txn rolled
        # back. Wire it at the in-memory factory so the test can
        # query the AuditEvent rows directly.
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = override_session_factory

    if current_user_id is not None:
        async def override_current_user() -> User:
            async with session_factory() as session:
                user = await session.get(User, current_user_id)
                assert user is not None
                return user

        app.dependency_overrides[get_current_user] = override_current_user

    app.include_router(auth_router)
    return app


async def _stepup_failure_rows(factory) -> list[AuditEvent]:
    async with factory() as db:
        result = await db.execute(
            select(AuditEvent).where(
                AuditEvent.event_type == "auth.google.sso_stepup.callback.failed"
            )
        )
        return list(result.scalars().all())


# ---------- helpers for the callback success path ---------------------------


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    """Stand-in for `httpx.AsyncClient` used in the step-up callback.

    Returns canned responses for the token-exchange POST and the
    userinfo GET. Tests parametrize the userinfo email to drive the
    "Google identity matches the seeded user" branch.

    `hang_on` ("post", "get" or "both") makes the named call sleep
    `delay_s` first, which is how the aggregate-timeout tests drive the
    bound. The sleep is `await asyncio.sleep(...)` and never
    `time.sleep(...)`: the fake runs on the TestClient's event loop, so
    a blocking sleep would wedge the suite rather than time out.

    `raise_exc` makes the token POST raise instead of answering, which
    is how S5 drives a non-timeout exception into the bounded block."""

    def __init__(
        self,
        *,
        userinfo_email: str,
        hang_on: str | None = None,
        delay_s: float = 0.0,
        raise_exc: BaseException | None = None,
    ):
        self._userinfo_email = userinfo_email
        self._hang_on = hang_on
        self._delay_s = delay_s
        self._raise_exc = raise_exc

    def __init__call(self, *_args, **_kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, *_args, **_kwargs):
        if self._hang_on in ("post", "both"):
            await asyncio.sleep(self._delay_s)
        if self._raise_exc is not None:
            raise self._raise_exc
        return _FakeResponse({"access_token": "fake-google-access-token"})

    async def get(self, *_args, **_kwargs):
        if self._hang_on in ("get", "both"):
            await asyncio.sleep(self._delay_s)
        return _FakeResponse(
            {
                "email": self._userinfo_email,
                "verified_email": True,
            }
        )


def _patch_httpx_for_email(
    monkeypatch,
    email: str,
    *,
    hang_on: str | None = None,
    delay_s: float = 0.0,
    raise_exc: BaseException | None = None,
) -> None:
    """Make the auth module's `httpx.AsyncClient(...)` build our fake.

    The router calls `httpx.AsyncClient(timeout=...)` then uses it as a
    context manager, so we replace the class with a factory closure
    that yields a fresh fake on every call."""

    def factory(*_args, **_kwargs):
        return _FakeAsyncClient(
            userinfo_email=email,
            hang_on=hang_on,
            delay_s=delay_s,
            raise_exc=raise_exc,
        )

    monkeypatch.setattr(auth_module.httpx, "AsyncClient", factory)


# ---------------------------------------------------------------------------
# Test 1 — no `return_to` in the body → default key in state, /settings.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_initiate_without_return_to_encodes_default_key(
    session_factory, google_config
):
    """When the request body omits `return_to`, the state cookie must
    encode the default key ("settings") in slot 4. The callback later
    keys off that slot, so the encoded value is what drives the
    redirect target."""
    user_id = await _seed_user(session_factory)
    app = _make_app(session_factory, user_id)

    with TestClient(app) as client:
        res = client.post("/api/v1/auth/sso-stepup/initiate")

    assert res.status_code == 200, res.text
    state_cookie = res.cookies.get("oauth_state")
    assert state_cookie is not None, "expected oauth_state cookie to be set"

    parts = state_cookie.split(":")
    assert parts[0] == "stepup"
    assert parts[1] == str(user_id)
    assert len(parts) == 4
    assert parts[3] == "settings"


@pytest.mark.asyncio
async def test_callback_with_default_state_redirects_to_settings(
    session_factory, google_config, monkeypatch
):
    """End-to-end pin: state with the default key → 302 to /settings
    (no `/security` suffix), with the issued step-up token in the URL
    fragment."""
    user_id = await _seed_user(session_factory, email="alice@acme.io")
    app = _make_app(session_factory, user_id)
    _patch_httpx_for_email(monkeypatch, "alice@acme.io")

    with TestClient(app) as client:
        # Run initiate so we have a matching state cookie+string.
        init = client.post("/api/v1/auth/sso-stepup/initiate")
        assert init.status_code == 200
        state = init.cookies.get("oauth_state")
        client.cookies.set("oauth_state", state)

        callback = client.get(
            "/api/v1/auth/sso-stepup/callback",
            params={"code": "fake-google-code", "state": state},
            follow_redirects=False,
        )

    assert callback.status_code == 302, callback.text
    location = callback.headers["location"]
    assert location.startswith("http://localhost/settings#stepup_token=")
    # Make sure it didn't accidentally land on /settings/security.
    assert "/settings/security" not in location


# ---------------------------------------------------------------------------
# Test 2 — `return_to: "security"` → /settings/security#stepup_token=<token>.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_initiate_with_security_return_to_encodes_security_key(
    session_factory, google_config
):
    user_id = await _seed_user(session_factory)
    app = _make_app(session_factory, user_id)

    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/sso-stepup/initiate",
            json={"return_to": "security"},
        )

    assert res.status_code == 200, res.text
    state_cookie = res.cookies.get("oauth_state")
    assert state_cookie is not None
    parts = state_cookie.split(":")
    assert parts[0] == "stepup"
    assert parts[1] == str(user_id)
    assert len(parts) == 4
    assert parts[3] == "security"


@pytest.mark.asyncio
async def test_callback_with_security_state_redirects_with_issued_token(
    session_factory, google_config, monkeypatch
):
    """Locks the headline invariant: a successful callback for the
    "security" target redirects to /settings/security#stepup_token=...
    where the fragment carries the same random token that was just
    written to `users.stepup_token`. The token in the URL must be the
    real issued token, not a placeholder."""
    user_id = await _seed_user(session_factory, email="alice@acme.io")
    app = _make_app(session_factory, user_id)
    _patch_httpx_for_email(monkeypatch, "alice@acme.io")

    with TestClient(app) as client:
        init = client.post(
            "/api/v1/auth/sso-stepup/initiate",
            json={"return_to": "security"},
        )
        assert init.status_code == 200
        state = init.cookies.get("oauth_state")
        client.cookies.set("oauth_state", state)

        callback = client.get(
            "/api/v1/auth/sso-stepup/callback",
            params={"code": "fake-google-code", "state": state},
            follow_redirects=False,
        )

    assert callback.status_code == 302, callback.text
    location = callback.headers["location"]
    assert location.startswith("http://localhost/settings/security#stepup_token=")

    # The token in the fragment must equal the one written to the row.
    fragment_token = location.split("#stepup_token=", 1)[1]
    assert fragment_token, "expected a non-empty step-up token in the fragment"

    async with session_factory() as db:
        user = await db.get(User, user_id)
        assert user is not None
        assert user.stepup_token == fragment_token
        assert user.stepup_token_expires_at is not None


# ---------------------------------------------------------------------------
# Test 3 — unknown `return_to` value → silently coerced to default. No
# attacker-controlled host or path ever reaches the redirect Location.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "evil_return_to",
    [
        "evil.example.com",
        "admin",
        "../",
        "//attacker.com",
    ],
)
@pytest.mark.asyncio
async def test_initiate_unknown_return_to_silently_coerces_to_default(
    session_factory, google_config, evil_return_to
):
    """The schema accepts arbitrary short strings; the handler validates
    against `_STEPUP_RETURN_TARGETS` and falls back to the default
    rather than 4xx, so old clients never break. The state must
    therefore encode "settings", never the attacker-supplied token."""
    user_id = await _seed_user(session_factory)
    app = _make_app(session_factory, user_id)

    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/sso-stepup/initiate",
            json={"return_to": evil_return_to},
        )

    assert res.status_code == 200, res.text
    state_cookie = res.cookies.get("oauth_state")
    assert state_cookie is not None
    parts = state_cookie.split(":")
    assert len(parts) == 4
    assert parts[3] == "settings"
    assert evil_return_to not in state_cookie

    # And the Google consent URL embeds the same coerced state, so the
    # round trip can't smuggle the attacker value back either.
    redirect_url = res.json()["redirect_url"]
    assert evil_return_to not in redirect_url


@pytest.mark.asyncio
async def test_callback_with_attacker_target_redirects_to_default(
    session_factory, google_config, monkeypatch
):
    """End-to-end pin: even when initiate is called with an attacker
    string, the callback redirect lands on /settings, never on the
    attacker-supplied path or host."""
    user_id = await _seed_user(session_factory, email="alice@acme.io")
    app = _make_app(session_factory, user_id)
    _patch_httpx_for_email(monkeypatch, "alice@acme.io")

    with TestClient(app) as client:
        init = client.post(
            "/api/v1/auth/sso-stepup/initiate",
            json={"return_to": "//attacker.com"},
        )
        assert init.status_code == 200
        state = init.cookies.get("oauth_state")
        client.cookies.set("oauth_state", state)

        callback = client.get(
            "/api/v1/auth/sso-stepup/callback",
            params={"code": "fake-google-code", "state": state},
            follow_redirects=False,
        )

    assert callback.status_code == 302, callback.text
    location = callback.headers["location"]
    assert location.startswith("http://localhost/settings#stepup_token=")
    assert "attacker.com" not in location


# ---------------------------------------------------------------------------
# Test 4 — malformed state at the callback returns 400 and never issues
# a step-up token.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_state",
    [
        "stepup:1:nonce-only-three-parts",  # legacy 3-part shape
        "nope",  # junk
        "stepup::nonce:settings",  # empty user_id slot
        "stepup:not-an-int:nonce:settings",  # non-numeric user_id
        "stepup:1:nonce:not-a-known-target",  # unknown return key
    ],
)
@pytest.mark.asyncio
async def test_callback_rejects_malformed_state(
    session_factory, google_config, bad_state
):
    """All variants must short-circuit with a friendly 307 redirect to
    /settings?sso_stepup_error=state (the front-line user error code
    here is "your sign-in attempt expired or didn't round-trip
    cleanly"), and must never write a step-up token onto any user row.

    Previously the handler raised 400. DO App Platform wraps that on a
    top-level GET navigation with a generic "Error / check logs" page,
    so we redirect instead and let the settings page render the right
    copy."""
    user_id = await _seed_user(session_factory)
    app = _make_app(session_factory, user_id)

    with TestClient(app) as client:
        client.cookies.set("oauth_state", bad_state)
        res = client.get(
            "/api/v1/auth/sso-stepup/callback",
            params={"code": "fake-google-code", "state": bad_state},
            follow_redirects=False,
        )

    assert res.status_code == 307, res.text
    location = res.headers.get("location", "")
    # All malformed-state variants resolve to the default /settings
    # landing — the unknown-return-key variant is the canary that the
    # fallback works even when state slot 4 is junk.
    assert location.endswith("/settings?sso_stepup_error=state"), location

    async with session_factory() as db:
        user = await db.get(User, user_id)
        assert user is not None
        assert user.stepup_token is None
        assert user.stepup_token_expires_at is None


@pytest.mark.asyncio
async def test_callback_with_empty_state_returns_friendly_redirect(
    session_factory, google_config
):
    """Empty state must not even reach the parser. The CSRF guard
    (`oauth_state` cookie matches the URL `state`) catches it first
    when the cookie is missing. Returns a friendly 307 redirect to
    /settings?sso_stepup_error=state rather than a 400, and never
    issues a step-up token."""
    user_id = await _seed_user(session_factory)
    app = _make_app(session_factory, user_id)

    with TestClient(app) as client:
        res = client.get(
            "/api/v1/auth/sso-stepup/callback",
            params={"code": "fake-google-code", "state": ""},
            follow_redirects=False,
        )

    assert res.status_code == 307, res.text
    location = res.headers.get("location", "")
    assert location.endswith("/settings?sso_stepup_error=state"), location

    async with session_factory() as db:
        user = await db.get(User, user_id)
        assert user is not None
        assert user.stepup_token is None


# ---------------------------------------------------------------------------
# Test 5 — Google cancel / missing code / provider error branches.
# These bypass FastAPI's old 422 by accepting code/state as Optional.
# The redirect target derives from `return_to` in state, so we run
# each branch for both /settings (default) and /settings/security.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stepup_callback_user_cancelled_redirects_to_settings(
    session_factory, google_config
):
    """User cancelled at Google. Default `return_to` (no /security)
    in state → friendly /settings redirect with banner-ready code."""
    user_id = await _seed_user(session_factory)
    app = _make_app(session_factory, user_id)
    state = f"stepup:{user_id}:nonce:settings"

    with TestClient(app) as client:
        # No oauth_state cookie — we want the cancelled branch to
        # surface a friendly redirect regardless of state validity.
        res = client.get(
            "/api/v1/auth/sso-stepup/callback",
            params={
                "error": "access_denied",
                "state": state,
                "error_description": "user denied consent",
            },
            follow_redirects=False,
        )

    assert res.status_code == 307, res.text
    location = res.headers.get("location", "")
    assert location.endswith("/settings?sso_stepup_error=cancelled"), location

    rows = await _stepup_failure_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].detail["reason"] == "cancelled"
    assert rows[0].detail["google_error"] == "access_denied"


@pytest.mark.asyncio
async def test_stepup_callback_user_cancelled_redirects_to_security(
    session_factory, google_config
):
    """Same cancel branch, but step-up initiated from /settings/security
    (`return_to: "security"`). Redirect must land on the security page,
    not /settings."""
    user_id = await _seed_user(session_factory)
    app = _make_app(session_factory, user_id)
    state = f"stepup:{user_id}:nonce:security"

    with TestClient(app) as client:
        res = client.get(
            "/api/v1/auth/sso-stepup/callback",
            params={"error": "access_denied", "state": state},
            follow_redirects=False,
        )

    assert res.status_code == 307, res.text
    location = res.headers.get("location", "")
    assert location.endswith(
        "/settings/security?sso_stepup_error=cancelled"
    ), location


@pytest.mark.asyncio
async def test_stepup_callback_provider_error_redirects_with_provider_error_code(
    session_factory, google_config
):
    """Non-cancel error (e.g. server_error) maps to provider_error so
    the banner copy reflects "Google had a problem", not "you cancelled"."""
    user_id = await _seed_user(session_factory)
    app = _make_app(session_factory, user_id)
    state = f"stepup:{user_id}:nonce:security"

    with TestClient(app) as client:
        res = client.get(
            "/api/v1/auth/sso-stepup/callback",
            params={"error": "server_error", "state": state},
            follow_redirects=False,
        )

    assert res.status_code == 307, res.text
    location = res.headers.get("location", "")
    assert location.endswith(
        "/settings/security?sso_stepup_error=provider_error"
    ), location

    rows = await _stepup_failure_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].detail["reason"] == "provider_error"
    assert rows[0].detail["google_error"] == "server_error"


@pytest.mark.asyncio
async def test_stepup_callback_missing_code_and_error_redirects_with_token_code(
    session_factory, google_config
):
    """Malformed callback: no code, no error. Reuse the existing
    "token" UI copy but audit the specific `missing_code` reason."""
    user_id = await _seed_user(session_factory)
    app = _make_app(session_factory, user_id)
    state = f"stepup:{user_id}:nonce:security"

    with TestClient(app) as client:
        res = client.get(
            "/api/v1/auth/sso-stepup/callback",
            params={"state": state},
            follow_redirects=False,
        )

    assert res.status_code == 307, res.text
    location = res.headers.get("location", "")
    assert location.endswith(
        "/settings/security?sso_stepup_error=token"
    ), location

    rows = await _stepup_failure_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].detail == {"reason": "missing_code"}


# ---------------------------------------------------------------------------
# Test 6 — the aggregate Google-exchange timeout at the step-up site.
#
# Step-up guards email change and first-password-set, so its failure
# path has its own audit event type, its own query param, its own cookie
# path and its own actor_email. These tests pin all four on the timeout
# branch: a "shared failure helper" that collapsed step-up onto the
# login site's `auth.google.callback.failed` event would silently empty
# the step-up half of /admin/audit with every other test still green.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stepup_token_post_timeout_redirects_and_audits_timeout(
    session_factory, google_config, monkeypatch
):
    """S1. Google never answers the step-up token POST.

    The aggregate bound must fire, audit `reason: "timeout"` under the
    step-up event type with the acting user's email attached, hand the
    user the existing `token` banner copy, and — critically — mint no
    step-up token. A step-up token issued on a failed exchange would be
    a bypass of the whole re-authentication gate.

    It must also clear the `oauth_state` cookie at the step-up path,
    the way every other step-up failure does. That deletion lives in
    `_stepup_failure` and nothing else in the suite red-gated it —
    removing the `delete_cookie` line left everything green — so it is
    pinned here. This is hygiene, not a functional retry fix:
    `sso_stepup_initiate` re-issues `oauth_state` at the same path on
    every retry, so a stale cookie would be overwritten rather than
    break the next attempt. The reason to keep it is that a
    single-use CSRF nonce should not outlive the exchange it
    authorised.
    """
    user_id = await _seed_user(session_factory, email="alice@acme.io")
    app = _make_app(session_factory, user_id)
    monkeypatch.setattr(auth_module, "GOOGLE_OAUTH_TOTAL_TIMEOUT_S", 0.05)
    _patch_httpx_for_email(monkeypatch, "alice@acme.io", hang_on="post", delay_s=2.0)

    started = time.monotonic()
    with TestClient(app) as client:
        init = client.post("/api/v1/auth/sso-stepup/initiate")
        assert init.status_code == 200
        state = init.cookies.get("oauth_state")
        client.cookies.set("oauth_state", state)

        res = client.get(
            "/api/v1/auth/sso-stepup/callback",
            params={"code": "fake-google-code", "state": state},
            follow_redirects=False,
        )
    elapsed = time.monotonic() - started

    assert res.status_code == 307, res.text
    location = res.headers.get("location", "")
    assert location.endswith("/settings?sso_stepup_error=token"), location
    assert elapsed < 1.0, elapsed

    set_cookies = res.headers.get_list("set-cookie")
    cleared = [c for c in set_cookies if c.startswith('oauth_state=""')]
    assert len(cleared) == 1, set_cookies
    assert "Path=/api/v1/auth/sso-stepup" in cleared[0], cleared[0]

    rows = await _stepup_failure_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].detail["reason"] == "timeout"
    assert rows[0].actor_email == "alice@acme.io"

    async with session_factory() as db:
        user = await db.get(User, user_id)
        assert user is not None
        assert user.stepup_token is None


@pytest.mark.asyncio
async def test_stepup_userinfo_get_timeout_records_last_phase(
    session_factory, google_config, monkeypatch
):
    """S2. The step-up token POST lands and the userinfo GET is wedged.

    A bound covering only the POST passes S1 and fails here.
    `last_phase` is what tells an operator which Google endpoint is
    stuck.
    """
    user_id = await _seed_user(session_factory, email="alice@acme.io")
    app = _make_app(session_factory, user_id)
    monkeypatch.setattr(auth_module, "GOOGLE_OAUTH_TOTAL_TIMEOUT_S", 0.05)
    _patch_httpx_for_email(monkeypatch, "alice@acme.io", hang_on="get", delay_s=2.0)

    started = time.monotonic()
    with TestClient(app) as client:
        init = client.post("/api/v1/auth/sso-stepup/initiate")
        assert init.status_code == 200
        state = init.cookies.get("oauth_state")
        client.cookies.set("oauth_state", state)

        res = client.get(
            "/api/v1/auth/sso-stepup/callback",
            params={"code": "fake-google-code", "state": state},
            follow_redirects=False,
        )
    elapsed = time.monotonic() - started

    assert res.status_code == 307, res.text
    assert res.headers.get("location", "").endswith(
        "/settings?sso_stepup_error=token"
    )
    assert elapsed < 1.0, elapsed

    rows = await _stepup_failure_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].detail["reason"] == "timeout"
    assert rows[0].detail["last_phase"] == "token_ok"


@pytest.mark.asyncio
async def test_stepup_two_individually_fast_calls_trip_the_shared_deadline(
    session_factory, google_config, monkeypatch
):
    """S3. The step-up bound is aggregate, not per call.

    Budget 0.6s; POST sleeps 0.4s and GET sleeps 0.4s. Neither call
    alone exceeds the budget, so a `timeout_at` reopened around each
    call lets both finish and the callback mints a step-up token. The
    0.4 / 0.6 ratio is load-bearing (50% headroom per call): tighten it
    and the wrong implementation times out too, and this test silently
    stops fencing anything.
    """
    user_id = await _seed_user(session_factory, email="alice@acme.io")
    app = _make_app(session_factory, user_id)
    monkeypatch.setattr(auth_module, "GOOGLE_OAUTH_TOTAL_TIMEOUT_S", 0.6)
    _patch_httpx_for_email(monkeypatch, "alice@acme.io", hang_on="both", delay_s=0.4)

    with TestClient(app) as client:
        init = client.post("/api/v1/auth/sso-stepup/initiate")
        assert init.status_code == 200
        state = init.cookies.get("oauth_state")
        client.cookies.set("oauth_state", state)

        res = client.get(
            "/api/v1/auth/sso-stepup/callback",
            params={"code": "fake-google-code", "state": state},
            follow_redirects=False,
        )

    location = res.headers.get("location", "")
    assert "sso_stepup_error=" in location, location
    assert "#stepup_token=" not in location, location

    rows = await _stepup_failure_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].detail["reason"] == "timeout"


@pytest.mark.asyncio
async def test_stepup_timeout_honours_the_security_return_target(
    session_factory, google_config, monkeypatch
):
    """S4. The timeout redirect must route through the same
    `_resolve_return_path(state)` every other step-up failure uses.

    A timeout branch that built its redirect inline with the hard-coded
    default would dump a user who started on /settings/security back on
    /settings, where no banner is listening — and would be the shape
    most likely to also drop the `oauth_state` cookie deletion that S1
    pins.
    """
    user_id = await _seed_user(session_factory, email="alice@acme.io")
    app = _make_app(session_factory, user_id)
    monkeypatch.setattr(auth_module, "GOOGLE_OAUTH_TOTAL_TIMEOUT_S", 0.05)
    _patch_httpx_for_email(monkeypatch, "alice@acme.io", hang_on="post", delay_s=2.0)

    with TestClient(app) as client:
        init = client.post(
            "/api/v1/auth/sso-stepup/initiate",
            json={"return_to": "security"},
        )
        assert init.status_code == 200
        state = init.cookies.get("oauth_state")
        client.cookies.set("oauth_state", state)

        res = client.get(
            "/api/v1/auth/sso-stepup/callback",
            params={"code": "fake-google-code", "state": state},
            follow_redirects=False,
        )

    assert res.status_code == 307, res.text
    location = res.headers.get("location", "")
    assert location.endswith("/settings/security?sso_stepup_error=token"), location


EXCHANGE_TIMEOUT_EVENT = "auth.google.callback.exchange_timeout"


def _exchange_timeout_warnings(logger_mock) -> list[dict]:
    """The kwargs of every `_LOGGER.warning(EXCHANGE_TIMEOUT_EVENT, ...)`
    call, in order. Same seam the breadcrumb tests use."""
    return [
        call.kwargs
        for call in logger_mock.warning.call_args_list
        if call.args and call.args[0] == EXCHANGE_TIMEOUT_EVENT
    ]


@pytest.mark.asyncio
async def test_stepup_exchange_timeout_emits_the_ungated_warning(
    session_factory, google_config, monkeypatch
):
    """Fence for the ungated timeout warning at `/sso-stepup/callback`.

    *Kills:* deleting the `_LOGGER.warning(...)` call from the step-up
    site's `except TimeoutError` clause.

    The two sites emit the same event name and are distinguished only
    by the `flow` field, so the step-up half is exactly the half a
    refactor can drop while the login fence stays green. `flow` is
    asserted for that reason: it is what lets an operator tell a wedged
    sign-in from a wedged email-change or first-password-set, and
    step-up is the security-sensitive one.

    `auth_debug_logging` is pinned False to prove the warning is
    ungated. `timeout_s` is asserted against the constant read back
    from `auth_module`, not a literal, so the test pins "the emitter
    reports the budget it actually used" without coupling to the
    harness value.

    The fields are asserted at the TOP level of the call kwargs, never
    under an `extra` key. `_LOGGER` is a structlog stdlib BoundLogger,
    which treats `extra` as an ordinary key and renders it as a nested
    object, so a DigitalOcean log filter on `flow:"stepup"` would not
    match one — and `flow` is the only thing separating this site from
    the login site. Pinning the flat shape is what keeps that
    distinction usable.
    """
    user_id = await _seed_user(session_factory, email="alice@acme.io")
    app = _make_app(session_factory, user_id)
    monkeypatch.setattr(app_settings, "auth_debug_logging", False)
    monkeypatch.setattr(auth_module, "GOOGLE_OAUTH_TOTAL_TIMEOUT_S", 0.05)
    _patch_httpx_for_email(monkeypatch, "alice@acme.io", hang_on="get", delay_s=2.0)

    with patch.object(auth_module, "_LOGGER") as logger_mock:
        with TestClient(app) as client:
            init = client.post("/api/v1/auth/sso-stepup/initiate")
            assert init.status_code == 200
            state = init.cookies.get("oauth_state")
            client.cookies.set("oauth_state", state)

            res = client.get(
                "/api/v1/auth/sso-stepup/callback",
                params={"code": "fake-google-code", "state": state},
                follow_redirects=False,
            )

    assert res.status_code == 307, res.text

    calls = _exchange_timeout_warnings(logger_mock)
    assert len(calls) == 1, calls
    fields = calls[0]
    assert "extra" not in fields, fields
    assert fields["flow"] == "stepup"
    assert fields["last_phase"] == "token_ok"
    assert fields["timeout_s"] == auth_module.GOOGLE_OAUTH_TOTAL_TIMEOUT_S


@pytest.mark.asyncio
async def test_stepup_exchange_timeout_warning_fires_on_the_timeout_path_only(
    session_factory, google_config, monkeypatch
):
    """Negative control: a successful step-up emits no timeout warning.

    *Kills:* the step-up emitter hoisted out of its `except
    TimeoutError` clause onto the main line or into a shared `finally`.
    """
    user_id = await _seed_user(session_factory, email="alice@acme.io")
    app = _make_app(session_factory, user_id)
    _patch_httpx_for_email(monkeypatch, "alice@acme.io")

    with patch.object(auth_module, "_LOGGER") as logger_mock:
        with TestClient(app) as client:
            init = client.post("/api/v1/auth/sso-stepup/initiate")
            assert init.status_code == 200
            state = init.cookies.get("oauth_state")
            client.cookies.set("oauth_state", state)

            res = client.get(
                "/api/v1/auth/sso-stepup/callback",
                params={"code": "fake-google-code", "state": state},
                follow_redirects=False,
            )

    assert res.status_code == 302, res.text
    assert _exchange_timeout_warnings(logger_mock) == []


class _ProgrammerBug(Exception):
    """A bug in our own code, not a transport failure."""


@pytest.mark.asyncio
async def test_stepup_non_timeout_exception_is_not_swallowed(
    session_factory, google_config, monkeypatch
):
    """S5. Fences the *upper* bound of the step-up `except` clause —
    the twin of L5 in test_auth_google_callback_errors.py.

    *Kills:* `except Exception:` in place of `except TimeoutError:` at
    the step-up site. Verified by injection: widening only the step-up
    clause leaves every other test in this file and in the login file
    green, because nothing else drives a non-timeout, non-httpx
    exception through this block.

    The concrete cost of that undetected mutation is not hypothetical:
    `tokens['access_token']` on the line between the two calls raises
    `KeyError` whenever Google's token payload lacks the key. Under a
    widened clause that becomes `?sso_stepup_error=token` plus an audit
    row recording `reason: "timeout"` — a row that blames Google for
    our own bug, on the flow that guards email change and
    first-password-set.

    A programmer error must propagate and must leave no audit row.
    """
    user_id = await _seed_user(session_factory, email="alice@acme.io")
    app = _make_app(session_factory, user_id)
    _patch_httpx_for_email(
        monkeypatch, "alice@acme.io", raise_exc=_ProgrammerBug("not a timeout")
    )

    with TestClient(app) as client:
        init = client.post("/api/v1/auth/sso-stepup/initiate")
        assert init.status_code == 200
        state = init.cookies.get("oauth_state")
        client.cookies.set("oauth_state", state)

        with pytest.raises(_ProgrammerBug):
            client.get(
                "/api/v1/auth/sso-stepup/callback",
                params={"code": "fake-google-code", "state": state},
                follow_redirects=False,
            )

    assert await _stepup_failure_rows(session_factory) == []
