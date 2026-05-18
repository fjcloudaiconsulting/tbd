"""Legacy refresh-cookie cleanup for the PR #211 path migration.

PR #211 (commit 70ddd26, 2026-05-11) widened the refresh cookie's Path
from ``/api/v1/auth/refresh`` to ``/``. Browsers carrying a pre-PR
cookie keep it indefinitely because ``delete_cookie(path="/")`` cannot
clear cookies set at the narrower path. The browser then sends BOTH
``refresh_token=`` entries on every /api/v1/auth/refresh request, and
Starlette's cookie parser picks only one — possibly the wrong one.

This test file pins two things:
  1. Every auth response that issues or clears the canonical Path=/
     cookie ALSO emits a Path=/api/v1/auth/refresh delete-cookie so the
     legacy cookie is actively retired.
  2. ``/refresh`` and ``/verify`` walk the full list of ``refresh_token``
     cookie values, accepting any that validates rather than blindly
     trusting whichever single value Starlette extracts.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import jwt as _pyjwt

from app.config import settings as app_settings
from app.database import get_db
from app.models import Base
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers.auth import LEGACY_REFRESH_COOKIE_PATH, router as auth_router
from app.security import create_refresh_token, hash_password
from tests.conftest import issue_test_refresh_token


def _mint_refresh_at(user_id: int, iat: datetime) -> str:
    """Mint a refresh JWT with a controlled ``iat`` (and matching
    ``session_created_at``) so tests can place tokens above or below
    ``token_cutoff`` deterministically without real sleeps.

    PR 2: stamp a fresh ``jti`` + ``sid`` AND seed the autouse fake
    Redis so the validation chain's primary-key probe accepts the
    token. Without the seed every legacy/current-cookie test in this
    file would 401 on ``"Session has been invalidated"`` regardless of
    the iat-vs-cutoff outcome.
    """
    import json
    import secrets as _secrets
    import uuid as _uuid

    from app import redis_client as _rc

    expire = iat + timedelta(days=app_settings.session_lifetime_days)
    jti = _secrets.token_urlsafe(16)
    sid = _uuid.uuid4().hex
    payload = {
        "sub": str(user_id),
        "type": "refresh",
        "session_created_at": iat.timestamp(),
        "iat": int(iat.timestamp()),
        "exp": expire,
        "jti": jti,
        "sid": sid,
    }
    token = _pyjwt.encode(
        payload, app_settings.jwt_secret_key, algorithm=app_settings.jwt_algorithm
    )
    client = _rc.get_client()
    if client is not None and hasattr(client, "_kv"):
        client._kv[f"auth:session:{jti}"] = json.dumps(
            {"user_id": user_id, "sid": sid}, separators=(",", ":")
        )
        client._sets[f"auth:session:by_sid:{sid}"].add(jti)
    return token


PASSWORD = "S3cret-Pass!"


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


@pytest.fixture(autouse=True)
def reset_limiter():
    limiter.reset()
    yield
    limiter.reset()


def make_app(session_factory) -> FastAPI:
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.include_router(auth_router)
    return app


async def _seed_user(factory, *, username: str = "alice") -> int:
    async with factory() as db:
        org = Organization(name="org", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        user = User(
            org_id=org.id,
            username=username,
            email=f"{username}@example.com",
            password_hash=hash_password(PASSWORD),
            role=Role.OWNER,
            is_superadmin=False,
            is_active=True,
            email_verified=True,
        )
        db.add(user)
        await db.commit()
        return user.id


def _set_cookie_values_for(headers, name: str) -> list[str]:
    """Return every Set-Cookie header whose cookie name is ``name``,
    in arrival order. Necessary because a single response may emit two
    Set-Cookie entries with the same name (one Path=/ set, one
    Path=/api/v1/auth/refresh delete)."""
    matches: list[str] = []
    raw_iter = headers.raw if hasattr(headers, "raw") else []
    for raw in raw_iter:
        if isinstance(raw, tuple):
            key, value = raw
            if key.decode().lower() != "set-cookie":
                continue
            value = value.decode()
        else:
            value = raw
        if value.split("=", 1)[0].strip().lower() == name.lower():
            matches.append(value)
    return matches


# ── Multi-cookie validation ────────────────────────────────────────────────


async def test_refresh_accepts_valid_when_legacy_invalid_present(session_factory):
    """Legacy cookie invalid (iat below cutoff) + current cookie valid
    → /refresh must succeed using the valid one. This is the canonical
    idle-return false-logout scenario."""
    user_id = await _seed_user(session_factory)

    # Anchor a cutoff timestamp halfway between the two iats so the
    # legacy token is rejected and the current one is accepted.
    # Both iats in the PAST (PyJWT rejects future-iat as
    # ImmatureSignatureError). Cutoff sits between them so legacy is
    # below cutoff (rejected) and current is above cutoff (accepted).
    now = datetime.now(timezone.utc)
    legacy = _mint_refresh_at(user_id, iat=now - timedelta(minutes=20))
    current = _mint_refresh_at(user_id, iat=now - timedelta(minutes=2))
    cutoff = now - timedelta(minutes=10)

    async with session_factory() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one()
        user.sessions_invalidated_at = cutoff
        await db.commit()

    app = make_app(session_factory)
    with TestClient(app) as client:
        # Browser sends BOTH cookies in one Cookie header. Legacy first
        # (path is more specific), current second.
        res = client.post(
            "/api/v1/auth/refresh",
            headers={"Cookie": f"refresh_token={legacy}; refresh_token={current}"},
        )

    assert res.status_code == 200, res.text
    assert "access_token" in res.json()


async def test_refresh_accepts_current_when_listed_first(session_factory):
    """Same as above, header order reversed (current first). Both orders
    must succeed — validator must walk the whole list, not depend on
    Starlette's pick."""
    user_id = await _seed_user(session_factory)

    # Both iats in the PAST (PyJWT rejects future-iat as
    # ImmatureSignatureError). Cutoff sits between them so legacy is
    # below cutoff (rejected) and current is above cutoff (accepted).
    now = datetime.now(timezone.utc)
    legacy = _mint_refresh_at(user_id, iat=now - timedelta(minutes=20))
    current = _mint_refresh_at(user_id, iat=now - timedelta(minutes=2))
    cutoff = now - timedelta(minutes=10)

    async with session_factory() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one()
        user.sessions_invalidated_at = cutoff
        await db.commit()

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/refresh",
            headers={"Cookie": f"refresh_token={current}; refresh_token={legacy}"},
        )

    assert res.status_code == 200, res.text


async def test_refresh_rejects_when_both_invalid(session_factory):
    """Both cookies invalid → 401 with the last failure's detail."""
    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/refresh",
            headers={"Cookie": "refresh_token=garbage1; refresh_token=garbage2"},
        )

    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid refresh token"


async def test_refresh_no_cookie_returns_existing_detail(session_factory):
    """Zero cookies → "No refresh token", matching pre-change behavior."""
    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post("/api/v1/auth/refresh")

    assert res.status_code == 401
    assert res.json()["detail"] == "No refresh token"


async def test_verify_accepts_valid_when_legacy_invalid_present(session_factory):
    """/verify must also walk the cookie list, and must NOT emit
    Set-Cookie on success even when a legacy cookie is present."""
    user_id = await _seed_user(session_factory)

    # Both iats in the PAST (PyJWT rejects future-iat as
    # ImmatureSignatureError). Cutoff sits between them so legacy is
    # below cutoff (rejected) and current is above cutoff (accepted).
    now = datetime.now(timezone.utc)
    legacy = _mint_refresh_at(user_id, iat=now - timedelta(minutes=20))
    current = _mint_refresh_at(user_id, iat=now - timedelta(minutes=2))
    cutoff = now - timedelta(minutes=10)

    async with session_factory() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one()
        user.sessions_invalidated_at = cutoff
        await db.commit()

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/verify",
            headers={"Cookie": f"refresh_token={legacy}; refresh_token={current}"},
        )

    assert res.status_code == 200, res.text
    # Load-bearing invariant: /verify never emits Set-Cookie, regardless
    # of how many refresh_token cookies the request carried.
    header_keys_lower = {k.lower() for k in res.headers.keys()}
    assert "set-cookie" not in header_keys_lower, (
        f"/verify must not emit Set-Cookie even when retiring a legacy cookie. "
        f"Got: {dict(res.headers)}"
    )


# ── Legacy-path cleanup on every set/delete site ───────────────────────────


def _assert_legacy_cleanup(headers):
    """Assert that the response emits a Set-Cookie deleting the legacy
    Path=/api/v1/auth/refresh refresh_token cookie."""
    cookies = _set_cookie_values_for(headers, "refresh_token")
    legacy_clear = [c for c in cookies if f"Path={LEGACY_REFRESH_COOKIE_PATH}" in c]
    assert legacy_clear, (
        f"Expected a Set-Cookie deleting refresh_token at "
        f"Path={LEGACY_REFRESH_COOKIE_PATH}. Got refresh_token Set-Cookies: {cookies}"
    )
    # Sanity: a delete-cookie carries Max-Age=0 (or expires in the past).
    raw = legacy_clear[0]
    assert "Max-Age=0" in raw or "expires=" in raw.lower(), (
        f"Legacy-path Set-Cookie should be a deletion. Got: {raw}"
    )


async def test_login_emits_legacy_cleanup(session_factory):
    await _seed_user(session_factory)

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": PASSWORD},
        )

    assert res.status_code == 200
    _assert_legacy_cleanup(res.headers)
    # And the canonical Path=/ set is still present.
    cookies = _set_cookie_values_for(res.headers, "refresh_token")
    canonical = [c for c in cookies if "Path=/" in c and f"Path={LEGACY_REFRESH_COOKIE_PATH}" not in c]
    assert canonical, f"login must still set the canonical Path=/ cookie. Got: {cookies}"


async def test_refresh_rotation_emits_legacy_cleanup(session_factory):
    user_id = await _seed_user(session_factory)
    refresh = issue_test_refresh_token(user_id)

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/refresh",
            cookies={"refresh_token": refresh},
        )

    assert res.status_code == 200
    _assert_legacy_cleanup(res.headers)


async def test_logout_emits_legacy_cleanup(session_factory):
    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post("/api/v1/auth/logout")

    assert res.status_code == 200
    _assert_legacy_cleanup(res.headers)


async def test_refresh_session_expired_emits_legacy_cleanup(session_factory):
    """Session-lifetime-expired path emits Set-Cookie to clear BOTH the
    canonical and the legacy cookie."""
    from app.config import settings as app_settings

    user_id = await _seed_user(session_factory)
    long_ago = datetime.now(timezone.utc) - timedelta(
        days=app_settings.session_lifetime_days + 30
    )
    refresh = issue_test_refresh_token(user_id, session_created_at=long_ago)

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/refresh",
            cookies={"refresh_token": refresh},
        )

    assert res.status_code == 401
    assert res.json()["detail"].startswith("Session expired")
    _assert_legacy_cleanup(res.headers)


# ── Ambiguous-session guard: refuse to silently pick one of two users ──────


async def test_refresh_rejects_when_two_valid_users_present(session_factory):
    """Two refresh cookies, each valid, each for a DIFFERENT user
    (e.g. legacy cookie belongs to account A, current cookie belongs to
    account B after an account switch). Auto-selecting either would
    silently authenticate the wrong identity, so the validator must
    refuse and force a clean re-login. /refresh must also emit
    delete-cookies for BOTH the canonical and the legacy path so the
    browser is fully reset."""
    user_a = await _seed_user(session_factory, username="alice")
    user_b = await _seed_user(session_factory, username="bob")

    token_a = issue_test_refresh_token(user_a)
    token_b = issue_test_refresh_token(user_b)

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/refresh",
            headers={"Cookie": f"refresh_token={token_a}; refresh_token={token_b}"},
        )

    assert res.status_code == 401
    assert res.json()["detail"] == "Ambiguous session — please sign in again"

    # Both the canonical Path=/ and the legacy
    # Path=/api/v1/auth/refresh cookies must be cleared so the browser
    # stops sending either of them.
    cookies = _set_cookie_values_for(res.headers, "refresh_token")
    canonical_clear = [
        c for c in cookies
        if "Path=/" in c
        and f"Path={LEGACY_REFRESH_COOKIE_PATH}" not in c
        and ("Max-Age=0" in c or "expires=" in c.lower())
    ]
    legacy_clear = [
        c for c in cookies
        if f"Path={LEGACY_REFRESH_COOKIE_PATH}" in c
        and ("Max-Age=0" in c or "expires=" in c.lower())
    ]
    assert canonical_clear, (
        f"ambiguous /refresh must clear canonical Path=/ cookie. Got: {cookies}"
    )
    assert legacy_clear, (
        f"ambiguous /refresh must clear legacy Path={LEGACY_REFRESH_COOKIE_PATH} cookie. Got: {cookies}"
    )


async def test_verify_rejects_when_two_valid_users_present(session_factory):
    """/verify must also refuse the ambiguous case, but MUST NOT emit
    any Set-Cookie (its no-Set-Cookie invariant is load-bearing for
    RSC). The follow-up /refresh from the client will do the cleanup."""
    user_a = await _seed_user(session_factory, username="carol")
    user_b = await _seed_user(session_factory, username="dave")

    token_a = issue_test_refresh_token(user_a)
    token_b = issue_test_refresh_token(user_b)

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/verify",
            headers={"Cookie": f"refresh_token={token_a}; refresh_token={token_b}"},
        )

    assert res.status_code == 401
    assert res.json()["detail"] == "Ambiguous session — please sign in again"

    header_keys_lower = {k.lower() for k in res.headers.keys()}
    assert "set-cookie" not in header_keys_lower, (
        f"/verify must not emit Set-Cookie on ambiguous-session 401. "
        f"Got: {dict(res.headers)}"
    )


async def test_refresh_prefers_newest_iat_for_same_user(session_factory):
    """Two valid refresh cookies for the SAME user (e.g. legacy + current
    after the PR #211 migration where both still validate). Selection
    must be deterministic and prefer the newer (higher ``iat``) token —
    the legacy cookie must never out-vote the current one. The newly
    issued refresh cookie carries forward the SAME session_created_at
    as the selected (newer) token."""
    user_id = await _seed_user(session_factory)

    # Both iats in the past so PyJWT accepts them; cutoff stays at
    # default (no sessions_invalidated_at) so both decode through to
    # successful validation.
    now = datetime.now(timezone.utc)
    older = _mint_refresh_at(user_id, iat=now - timedelta(minutes=30))
    newer = _mint_refresh_at(user_id, iat=now - timedelta(minutes=1))

    # Decode the newer token to learn its session_created_at — the
    # rotation should preserve it.
    import jwt as _pyjwt
    newer_payload = _pyjwt.decode(
        newer, app_settings.jwt_secret_key, algorithms=[app_settings.jwt_algorithm]
    )
    newer_session_created_at = int(newer_payload["session_created_at"])

    app = make_app(session_factory)
    with TestClient(app) as client:
        # Legacy/older first (browser send order), newer second.
        res = client.post(
            "/api/v1/auth/refresh",
            headers={"Cookie": f"refresh_token={older}; refresh_token={newer}"},
        )

    assert res.status_code == 200, res.text

    # The rotated refresh cookie carries forward the NEWER session
    # marker. Decode the new cookie and assert.
    cookies = _set_cookie_values_for(res.headers, "refresh_token")
    canonical = [
        c for c in cookies
        if "Path=/" in c
        and f"Path={LEGACY_REFRESH_COOKIE_PATH}" not in c
        and "Max-Age=0" not in c
    ]
    assert canonical, f"refresh must set a new canonical cookie. Got: {cookies}"
    new_cookie_value = canonical[0].split(";", 1)[0].split("=", 1)[1]
    rotated_payload = _pyjwt.decode(
        new_cookie_value,
        app_settings.jwt_secret_key,
        algorithms=[app_settings.jwt_algorithm],
    )
    assert int(rotated_payload["session_created_at"]) == newer_session_created_at, (
        "rotation should carry forward the NEWER token's session_created_at, "
        f"not the older one's. Got: {rotated_payload}"
    )


async def test_refresh_prefers_newest_iat_when_order_reversed(session_factory):
    """Selection is by iat, not by header position."""
    user_id = await _seed_user(session_factory)

    now = datetime.now(timezone.utc)
    older = _mint_refresh_at(user_id, iat=now - timedelta(minutes=30))
    newer = _mint_refresh_at(user_id, iat=now - timedelta(minutes=1))

    import jwt as _pyjwt
    newer_payload = _pyjwt.decode(
        newer, app_settings.jwt_secret_key, algorithms=[app_settings.jwt_algorithm]
    )
    newer_session_created_at = int(newer_payload["session_created_at"])

    app = make_app(session_factory)
    with TestClient(app) as client:
        # Newer first, older second.
        res = client.post(
            "/api/v1/auth/refresh",
            headers={"Cookie": f"refresh_token={newer}; refresh_token={older}"},
        )

    assert res.status_code == 200, res.text
    cookies = _set_cookie_values_for(res.headers, "refresh_token")
    canonical = [
        c for c in cookies
        if "Path=/" in c
        and f"Path={LEGACY_REFRESH_COOKIE_PATH}" not in c
        and "Max-Age=0" not in c
    ]
    new_cookie_value = canonical[0].split(";", 1)[0].split("=", 1)[1]
    rotated_payload = _pyjwt.decode(
        new_cookie_value,
        app_settings.jwt_secret_key,
        algorithms=[app_settings.jwt_algorithm],
    )
    assert int(rotated_payload["session_created_at"]) == newer_session_created_at


# ── _issue_tokens cleanup (MFA verify, etc.) ───────────────────────────────


@pytest.mark.asyncio
async def test_issue_tokens_helper_emits_legacy_cleanup():
    """``_issue_tokens`` is the shared exit point for several MFA login
    flows (``/mfa/verify``, ``/mfa/recovery``, and ``/mfa/email-verify``).
    Pinning the helper directly avoids the cost of a full MFA-setup
    fixture while still proving the cleanup is wired.

    PR 2 made ``_issue_tokens`` async because it now writes the Redis
    primary key + family set before returning. The autouse fake-Redis
    fixture in ``conftest.py`` keeps this test working without a real
    Redis dependency.
    """
    from fastapi import Response
    from app.routers.auth import _issue_tokens

    class _FakeUser:
        def __init__(self):
            self.id = 1
            self.org_id = 1
            self.role = Role.OWNER

    class _FakeDb:
        """Stand-in for ``AsyncSession`` — ``_issue_tokens`` calls
        ``db.scalar(...)`` to resolve the per-org TTL, falling back to
        the system default when ``None`` is returned. That's exactly
        the behaviour we want here so the test stays focused on the
        legacy-cookie cleanup signal."""

        async def scalar(self, _query):
            return None

    response = Response()
    await _issue_tokens(_FakeUser(), response, _FakeDb())

    # Response.raw_headers is the underlying list of (bytes, bytes)
    # tuples populated by set_cookie / delete_cookie.
    cookies = [v.decode() for k, v in response.raw_headers if k == b"set-cookie"]
    refresh_cookies = [c for c in cookies if c.startswith("refresh_token=")]
    legacy_clear = [
        c for c in refresh_cookies if f"Path={LEGACY_REFRESH_COOKIE_PATH}" in c
    ]
    assert legacy_clear, (
        f"_issue_tokens must emit legacy-path cleanup Set-Cookie. "
        f"Got refresh_token Set-Cookies: {refresh_cookies}"
    )


# ── Google SSO callback cleanup ────────────────────────────────────────────


@pytest.fixture
def google_config(monkeypatch):
    """Populate the Google OAuth env so ``_validate_google_config`` passes."""
    monkeypatch.setattr(app_settings, "google_client_id", "test-client-id")
    monkeypatch.setattr(app_settings, "google_client_secret", "test-client-secret")
    monkeypatch.setattr(app_settings, "app_url", "http://localhost")
    yield


async def test_google_callback_emits_legacy_cleanup(
    session_factory, google_config, monkeypatch
):
    """The Google SSO callback writes the refresh cookie onto a
    directly-returned RedirectResponse. Pin that the legacy-path
    cleanup rides along."""
    # Seed a default plan so ``create_trial`` (new-user branch) does
    # not 500, and seed an existing user matching the canned email so
    # the callback takes the existing-user branch (no plan required,
    # but cheap insurance).
    from app.models.subscription import Plan
    async with session_factory() as db:
        existing = await db.scalar(select(Plan).where(Plan.slug == "free"))
        if existing is None:
            db.add(Plan(slug="free", name="Free", is_active=True, sort_order=0))
            await db.commit()
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        db.add(User(
            org_id=org.id,
            username="alice",
            email="alice@acme.io",
            password_hash=hash_password(PASSWORD),
            role=Role.OWNER,
            is_active=True,
            email_verified=True,
        ))
        await db.commit()

    # Mock httpx so the callback's token-exchange and userinfo calls
    # return a successful verified-email payload for alice@acme.io.
    from app.routers import auth as auth_module

    class _FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def post(self, *args, **kwargs):
            return _FakeResponse(200, {"access_token": "fake-token"})

        async def get(self, *args, **kwargs):
            return _FakeResponse(200, {
                "email": "alice@acme.io",
                "verified_email": True,
                "given_name": "Alice",
                "family_name": "A",
            })

    monkeypatch.setattr(auth_module.httpx, "AsyncClient", _FakeClient)

    # Wire the session-factory dependency so audit writes hit the
    # in-memory DB (Google callback emits success audit).
    from app.deps import get_session_factory

    app = make_app(session_factory)

    async def _override_session_factory():
        return session_factory

    app.dependency_overrides[get_session_factory] = _override_session_factory

    with TestClient(app) as client:
        client.cookies.set("oauth_state", "matching-state")
        res = client.get(
            "/api/v1/auth/google/callback",
            params={"code": "dummy", "state": "matching-state"},
            follow_redirects=False,
        )

    assert res.status_code == 302, res.text
    _assert_legacy_cleanup(res.headers)


# ── Invitation accept cleanup ──────────────────────────────────────────────


async def test_invite_accept_emits_legacy_cleanup(session_factory, monkeypatch):
    """``POST /api/v1/orgs/invitations/accept`` sets its own refresh
    cookie on success (``backend/app/routers/org_members.py``). The
    legacy cleanup must ride along on that response too."""
    from app.routers import org_members as org_members_module
    from app.routers.org_members import router as org_members_router

    user_id = await _seed_user(session_factory, username="invitee")

    # Monkeypatch the service to return a real user without needing to
    # construct the full invitation/token plumbing. The router is what
    # we're pinning here, not the service.
    async def _fake_accept(db, *, token, username, password):
        result = await db.execute(select(User).where(User.id == user_id))
        return result.scalar_one()

    monkeypatch.setattr(
        org_members_module.invitation_service, "accept_invitation", _fake_accept
    )

    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.include_router(org_members_router)

    with TestClient(app) as client:
        res = client.post(
            "/api/v1/orgs/invitations/accept",
            json={"token": "fake-token", "username": "invitee", "password": PASSWORD},
        )

    assert res.status_code == 200, res.text
    _assert_legacy_cleanup(res.headers)
