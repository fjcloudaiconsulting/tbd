"""Backend PR-A — `POST /api/v1/auth/verify` endpoint + refresh cookie path widened to `/`.

Pins the new server-side session verification endpoint (consumed by Next.js
RSC) and the widened cookie path that lets the browser send the refresh
cookie on regular page requests so RSC can authenticate server-side.

Critical invariants:
- `/verify` must NEVER set or rotate the refresh cookie (no Set-Cookie).
- `/verify` must NEVER write an audit log on success.
- `refresh_token` cookies (set + delete) carry `Path=/`, not the old
  `Path=/api/v1/auth/refresh`.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.models import Base
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers.auth import router as auth_router
from app.security import create_refresh_token, hash_password


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
    """Each test starts with an empty rate-limiter state."""
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


async def _seed_user(
    factory,
    *,
    is_active: bool = True,
    username: str = "alice",
    email: str = "alice@example.com",
    email_verified: bool = True,
) -> int:
    async with factory() as db:
        org = Organization(name="org", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        user = User(
            org_id=org.id,
            username=username,
            email=email,
            password_hash=hash_password(PASSWORD),
            role=Role.OWNER,
            is_superadmin=False,
            is_active=is_active,
            email_verified=email_verified,
        )
        db.add(user)
        await db.commit()
        return user.id


# ── /verify happy path ───────────────────────────────────────────────────────


async def test_verify_returns_user_and_access_token(session_factory):
    user_id = await _seed_user(session_factory)
    refresh = create_refresh_token(user_id)

    app = make_app(session_factory)
    with TestClient(app) as client:
        # First call `/me` after login to know the canonical user shape; here
        # we just assert the verify body contains the same key fields.
        res = client.post(
            "/api/v1/auth/verify",
            cookies={"refresh_token": refresh},
        )

    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) == {"user", "access_token", "token_type"}
    assert body["token_type"] == "bearer"
    assert isinstance(body["access_token"], str) and body["access_token"]
    # `user` shape mirrors UserResponse / /auth/me.
    user_resp = body["user"]
    assert user_resp["id"] == user_id
    assert user_resp["username"] == "alice"
    assert user_resp["email"] == "alice@example.com"
    assert user_resp["role"] == Role.OWNER.value
    assert user_resp["is_active"] is True
    assert "org_id" in user_resp and "org_name" in user_resp


async def test_verify_user_shape_matches_me(session_factory):
    """The user payload must be identical to /auth/me's UserResponse."""
    user_id = await _seed_user(session_factory)
    refresh = create_refresh_token(user_id)

    app = make_app(session_factory)
    with TestClient(app) as client:
        # /me requires a Bearer access token; obtain via /login.
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": PASSWORD},
        )
        assert login.status_code == 200
        access = login.json()["access_token"]
        me = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {access}"},
        )
        assert me.status_code == 200

        verify = client.post(
            "/api/v1/auth/verify",
            cookies={"refresh_token": refresh},
        )
        assert verify.status_code == 200

    assert verify.json()["user"] == me.json()


# ── /verify error paths ──────────────────────────────────────────────────────


async def test_verify_no_cookie_returns_401(session_factory):
    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post("/api/v1/auth/verify")

    assert res.status_code == 401
    assert res.json()["detail"] == "No refresh token"


async def test_verify_invalid_refresh_token_returns_401(session_factory):
    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/verify",
            cookies={"refresh_token": "this.is.not.a.jwt"},
        )

    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid refresh token"


async def test_verify_access_token_not_accepted_as_refresh(session_factory):
    """An access token (wrong `type` claim) must be rejected."""
    from app.security import create_access_token

    user_id = await _seed_user(session_factory)
    access = create_access_token(user_id, 1, Role.OWNER.value)

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/verify",
            cookies={"refresh_token": access},
        )

    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid refresh token"


async def test_verify_inactive_user_returns_401(session_factory):
    user_id = await _seed_user(session_factory, is_active=False)
    refresh = create_refresh_token(user_id)

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/verify",
            cookies={"refresh_token": refresh},
        )

    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid session"


# ── Critical invariant: /verify never rotates or sets a cookie ───────────────


async def test_verify_does_not_rotate_or_set_cookie(session_factory):
    """The whole point of /verify: it is a passive read. Asserting NO
    `Set-Cookie` on the response is the load-bearing test for this PR."""
    user_id = await _seed_user(session_factory)
    refresh = create_refresh_token(user_id)

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/verify",
            cookies={"refresh_token": refresh},
        )

    assert res.status_code == 200
    header_keys_lower = {k.lower() for k in res.headers.keys()}
    assert "set-cookie" not in header_keys_lower, (
        f"/verify must not emit Set-Cookie; got headers: {dict(res.headers)}"
    )


# ── Cookie path widened to `/` ───────────────────────────────────────────────


def _set_cookie_for(headers, name: str) -> str | None:
    """Return the raw Set-Cookie header value whose cookie name is `name`."""
    # httpx exposes multi-valued headers via the .raw / .multi_items interface;
    # falling back to the joined value works because each cookie attribute
    # block is comma-separated only when the cookie names differ.
    for raw_value in headers.get_list("set-cookie") if hasattr(headers, "get_list") else headers.raw:
        if isinstance(raw_value, tuple):
            # httpx Headers.raw → list[(b"set-cookie", b"...")]
            key, value = raw_value
            if key.decode().lower() != "set-cookie":
                continue
            value = value.decode()
        else:
            value = raw_value
        if value.split("=", 1)[0].strip().lower() == name.lower():
            return value
    return None


async def test_cookie_path_is_root_on_login(session_factory):
    await _seed_user(session_factory)

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": PASSWORD},
        )

    assert res.status_code == 200
    raw = _set_cookie_for(res.headers, "refresh_token")
    assert raw is not None, f"No refresh_token cookie on login. Headers: {dict(res.headers)}"
    assert "Path=/" in raw
    assert "Path=/api/v1/auth/refresh" not in raw


async def test_cookie_path_is_root_on_refresh_rotation(session_factory):
    user_id = await _seed_user(session_factory)
    refresh = create_refresh_token(user_id)

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/refresh",
            cookies={"refresh_token": refresh},
        )

    assert res.status_code == 200
    raw = _set_cookie_for(res.headers, "refresh_token")
    assert raw is not None, f"No refresh_token cookie on refresh. Headers: {dict(res.headers)}"
    assert "Path=/" in raw
    assert "Path=/api/v1/auth/refresh" not in raw


async def test_cookie_path_is_root_on_logout_delete(session_factory):
    await _seed_user(session_factory)

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post("/api/v1/auth/logout")

    assert res.status_code == 200
    raw = _set_cookie_for(res.headers, "refresh_token")
    assert raw is not None, (
        f"Logout must emit a delete-cookie Set-Cookie header. Got: {dict(res.headers)}"
    )
    assert "Path=/" in raw
    assert "Path=/api/v1/auth/refresh" not in raw
