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


def _mint_refresh_at(user_id: int, iat: datetime) -> str:
    """Mint a refresh JWT with a controlled ``iat`` (and matching
    ``session_created_at``) so tests can place tokens above or below
    ``token_cutoff`` deterministically without real sleeps."""
    expire = iat + timedelta(days=app_settings.jwt_refresh_token_expire_days)
    payload = {
        "sub": str(user_id),
        "type": "refresh",
        "session_created_at": iat.timestamp(),
        "iat": int(iat.timestamp()),
        "exp": expire,
    }
    return _pyjwt.encode(
        payload, app_settings.jwt_secret_key, algorithm=app_settings.jwt_algorithm
    )


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
    refresh = create_refresh_token(user_id)

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
    from app.security import create_refresh_token as _crt

    user_id = await _seed_user(session_factory)
    long_ago = datetime.now(timezone.utc) - timedelta(
        days=app_settings.session_lifetime_days + 30
    )
    refresh = _crt(user_id, session_created_at=long_ago)

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/refresh",
            cookies={"refresh_token": refresh},
        )

    assert res.status_code == 401
    assert res.json()["detail"].startswith("Session expired")
    _assert_legacy_cleanup(res.headers)
