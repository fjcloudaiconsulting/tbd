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

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_current_user
from app.models import Base
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

    app.dependency_overrides[get_db] = override_get_db

    if current_user_id is not None:
        async def override_current_user() -> User:
            async with session_factory() as session:
                user = await session.get(User, current_user_id)
                assert user is not None
                return user

        app.dependency_overrides[get_current_user] = override_current_user

    app.include_router(auth_router)
    return app


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
    "Google identity matches the seeded user" branch."""

    def __init__(self, *, userinfo_email: str):
        self._userinfo_email = userinfo_email

    def __init__call(self, *_args, **_kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, *_args, **_kwargs):
        return _FakeResponse({"access_token": "fake-google-access-token"})

    async def get(self, *_args, **_kwargs):
        return _FakeResponse(
            {
                "email": self._userinfo_email,
                "verified_email": True,
            }
        )


def _patch_httpx_for_email(monkeypatch, email: str) -> None:
    """Make the auth module's `httpx.AsyncClient(...)` build our fake.

    The router calls `httpx.AsyncClient(timeout=...)` then uses it as a
    context manager, so we replace the class with a factory closure
    that yields a fresh fake on every call."""

    def factory(*_args, **_kwargs):
        return _FakeAsyncClient(userinfo_email=email)

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

        callback = client.get(
            "/api/v1/auth/sso-stepup/callback",
            params={"code": "fake-google-code", "state": state},
            cookies={"oauth_state": state},
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

        callback = client.get(
            "/api/v1/auth/sso-stepup/callback",
            params={"code": "fake-google-code", "state": state},
            cookies={"oauth_state": state},
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

        callback = client.get(
            "/api/v1/auth/sso-stepup/callback",
            params={"code": "fake-google-code", "state": state},
            cookies={"oauth_state": state},
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
    """All variants must short-circuit with 400 "Malformed step-up
    state" (or "Invalid OAuth state ..." when the cookie does not
    match), and must never write a step-up token onto any user row."""
    user_id = await _seed_user(session_factory)
    app = _make_app(session_factory, user_id)

    with TestClient(app) as client:
        res = client.get(
            "/api/v1/auth/sso-stepup/callback",
            params={"code": "fake-google-code", "state": bad_state},
            cookies={"oauth_state": bad_state},
            follow_redirects=False,
        )

    assert res.status_code == 400, res.text
    assert res.headers.get("location") is None
    detail = res.json().get("detail", "")
    assert "Malformed step-up state" in detail or "Invalid state" in detail

    async with session_factory() as db:
        user = await db.get(User, user_id)
        assert user is not None
        assert user.stepup_token is None
        assert user.stepup_token_expires_at is None


@pytest.mark.asyncio
async def test_callback_with_empty_state_returns_400(session_factory, google_config):
    """Empty state must not even reach the parser. The CSRF guard
    (`oauth_state` cookie matches the URL `state`) catches it first
    when the cookie is missing, and the 4-part shape check catches it
    if a stray cookie sneaks through. Either way: 400, no redirect,
    no token issued."""
    user_id = await _seed_user(session_factory)
    app = _make_app(session_factory, user_id)

    with TestClient(app) as client:
        res = client.get(
            "/api/v1/auth/sso-stepup/callback",
            params={"code": "fake-google-code", "state": ""},
            follow_redirects=False,
        )

    assert res.status_code == 400, res.text
    assert res.headers.get("location") is None

    async with session_factory() as db:
        user = await db.get(User, user_id)
        assert user is not None
        assert user.stepup_token is None
