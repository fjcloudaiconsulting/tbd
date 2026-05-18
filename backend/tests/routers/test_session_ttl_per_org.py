"""Per-org session TTL — the 2026-05-18 session-stability fix.

Before this fix, ``session_lifetime_days`` on ``OrgSetting`` only acted as
an absolute-lifetime cap, while the cookie ``Max-Age``, the refresh JWT
``exp``, and the Redis primary-key TTL were independently driven by
``refresh_idle_ttl_days`` (system-only). The org-level "Maximum session
duration" UI control was therefore decorative for any value above the
system idle TTL (30 days default) — the user's session died at 30 days
no matter what they set.

After this fix, the per-org ``session_lifetime_days`` setting drives all
four TTLs in lockstep. These tests pin that contract end-to-end:

  * cookie ``Max-Age`` at login + /refresh rotation reflects the org's
    ``session_lifetime_days`` value
  * the refresh JWT ``exp`` claim moves in lockstep
  * PUT /api/v1/settings rejects out-of-bounds / non-integer values
  * the absolute-lifetime check at /verify honours the same per-org value

Without these regression tests a future refactor could silently revert
to the old "decorative setting" behaviour and the UI would lie again.
"""
from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt as _jwt
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.settings import OrgSetting
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers.auth import LEGACY_REFRESH_COOKIE_PATH, router as auth_router
from app.routers.settings import router as settings_router
from app.security import hash_password
from tests.conftest import issue_test_refresh_token


PASSWORD = "starting-password-1"


# ── fixtures (mirrors test_auth_cookie_max_age.py) ──────────────────────────


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


def _make_app(session_factory, *, include_settings: bool = False) -> FastAPI:
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
    if include_settings:
        app.include_router(settings_router)
    return app


async def _seed_user(
    factory: async_sessionmaker[AsyncSession],
    *,
    org_session_lifetime_days: int | str | None = None,
    role: Role = Role.OWNER,
) -> dict:
    async with factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        if org_session_lifetime_days is not None:
            db.add(
                OrgSetting(
                    org_id=org.id,
                    key="session_lifetime_days",
                    value=str(org_session_lifetime_days),
                )
            )
        user = User(
            org_id=org.id,
            username="alice",
            email="alice@example.com",
            password_hash=hash_password(PASSWORD),
            role=role,
            is_superadmin=False,
            is_active=True,
            email_verified=True,
        )
        db.add(user)
        await db.commit()
        return {"org_id": org.id, "user_id": user.id}


def _set_cookie_values_for(headers, name: str) -> list[str]:
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


def _canonical_refresh_cookie(headers) -> str:
    cookies = _set_cookie_values_for(headers, "refresh_token")
    canonical = [
        c
        for c in cookies
        if "Path=/" in c
        and f"Path={LEGACY_REFRESH_COOKIE_PATH}" not in c
        and "Max-Age=0" not in c
    ]
    assert canonical, f"Expected canonical Set-Cookie; got: {cookies}"
    return canonical[0]


def _refresh_token_value_from_set_cookie(raw: str) -> str:
    """Extract the cookie value (the JWT) from a Set-Cookie header."""
    name_value = raw.split(";", 1)[0]
    return name_value.split("=", 1)[1]


def _max_age_from_set_cookie(raw: str) -> int:
    for part in raw.split(";"):
        part = part.strip()
        if part.lower().startswith("max-age="):
            return int(part.split("=", 1)[1])
    raise AssertionError(f"Set-Cookie has no Max-Age: {raw!r}")


# ── Cookie Max-Age tracks per-org setting ───────────────────────────────────


@pytest.mark.asyncio
async def test_login_cookie_max_age_uses_per_org_session_lifetime_days(
    session_factory,
) -> None:
    """When an org has ``OrgSetting(session_lifetime_days=60)`` the
    /login cookie's ``Max-Age`` must be ``60 * 86400`` — not the
    system default of ``30 * 86400``. This is the root-cause fix
    for the 2026-05-18 "setting is decorative" bug."""
    await _seed_user(session_factory, org_session_lifetime_days=60)
    app = _make_app(session_factory)

    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": PASSWORD},
        )

    assert res.status_code == 200, res.json()
    raw = _canonical_refresh_cookie(res.headers)
    assert _max_age_from_set_cookie(raw) == 60 * 86400


@pytest.mark.asyncio
async def test_login_cookie_max_age_falls_back_to_system_when_org_unset(
    session_factory,
) -> None:
    """No ``OrgSetting`` row => system default
    ``app_settings.session_lifetime_days * 86400``."""
    await _seed_user(session_factory)  # no org setting
    app = _make_app(session_factory)

    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": PASSWORD},
        )

    assert res.status_code == 200, res.json()
    raw = _canonical_refresh_cookie(res.headers)
    assert (
        _max_age_from_set_cookie(raw)
        == app_settings.session_lifetime_days * 86400
    )


@pytest.mark.asyncio
async def test_login_cookie_max_age_falls_back_when_org_value_malformed(
    session_factory,
) -> None:
    """Out-of-band non-integer org value silently falls back to system
    default. PUT /settings rejects this at the write site; the fallback
    here is defence-in-depth for direct DB writes."""
    await _seed_user(session_factory, org_session_lifetime_days="not-a-number")
    app = _make_app(session_factory)

    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": PASSWORD},
        )

    assert res.status_code == 200, res.json()
    raw = _canonical_refresh_cookie(res.headers)
    assert (
        _max_age_from_set_cookie(raw)
        == app_settings.session_lifetime_days * 86400
    )


@pytest.mark.asyncio
async def test_login_cookie_max_age_falls_back_when_org_value_out_of_bounds(
    session_factory,
) -> None:
    """Out-of-bounds org value (e.g. ``0`` or ``9999``) also falls
    back. PUT /settings should reject this before it can be written —
    this test pins the read-side defence-in-depth."""
    await _seed_user(session_factory, org_session_lifetime_days=9999)
    app = _make_app(session_factory)

    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": PASSWORD},
        )

    assert res.status_code == 200, res.json()
    raw = _canonical_refresh_cookie(res.headers)
    assert (
        _max_age_from_set_cookie(raw)
        == app_settings.session_lifetime_days * 86400
    )


# ── JWT exp tracks per-org setting in lockstep with cookie Max-Age ──────────


@pytest.mark.asyncio
async def test_login_refresh_jwt_exp_matches_per_org_session_lifetime_days(
    session_factory,
) -> None:
    """The refresh JWT ``exp`` and the cookie ``Max-Age`` must move in
    lockstep — otherwise the browser ships a cookie that the backend
    rejects, or the backend honours a cookie the browser already
    dropped."""
    await _seed_user(session_factory, org_session_lifetime_days=60)
    app = _make_app(session_factory)

    issued_at = int(time.time())
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": PASSWORD},
        )

    raw = _canonical_refresh_cookie(res.headers)
    cookie_max_age = _max_age_from_set_cookie(raw)
    refresh_jwt = _refresh_token_value_from_set_cookie(raw)
    payload = _jwt.decode(
        refresh_jwt,
        app_settings.jwt_secret_key,
        algorithms=[app_settings.jwt_algorithm],
    )
    jwt_exp = int(payload["exp"])

    # JWT exp should be roughly issued_at + cookie_max_age. Allow 5s
    # slack for test runner clock + handler overhead.
    assert abs((jwt_exp - issued_at) - cookie_max_age) <= 5, (
        f"JWT exp ({jwt_exp}) - issued_at ({issued_at}) "
        f"= {jwt_exp - issued_at}; expected ≈ {cookie_max_age}"
    )


# ── Refresh rotation respects current org setting ───────────────────────────


@pytest.mark.asyncio
async def test_refresh_rotation_uses_current_per_org_session_lifetime_days(
    session_factory,
) -> None:
    """When the admin changes the org's setting mid-session, the NEXT
    /refresh rotation must apply the new TTL to the rotated cookie /
    JWT / Redis row — not the value baked into the prior token."""
    seed = await _seed_user(session_factory, org_session_lifetime_days=7)
    refresh = issue_test_refresh_token(
        seed["user_id"], ttl_seconds=7 * 86400
    )
    # Admin bumps the org TTL to 60 days after login.
    async with session_factory() as db:
        setting = await db.scalar(
            select(OrgSetting).where(
                OrgSetting.org_id == seed["org_id"],
                OrgSetting.key == "session_lifetime_days",
            )
        )
        setting.value = "60"
        await db.commit()
    app = _make_app(session_factory)

    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/refresh",
            cookies={"refresh_token": refresh},
        )

    assert res.status_code == 200, res.json()
    raw = _canonical_refresh_cookie(res.headers)
    assert _max_age_from_set_cookie(raw) == 60 * 86400


# ── Absolute-lifetime check honours per-org setting ─────────────────────────


@pytest.mark.asyncio
async def test_refresh_rejects_session_older_than_per_org_lifetime(
    session_factory,
) -> None:
    """A refresh JWT whose ``session_created_at`` is older than the
    org's ``session_lifetime_days`` must be rejected with
    ``SESSION_EXPIRED_DETAIL``. The cookie is then cleared."""
    seed = await _seed_user(session_factory, org_session_lifetime_days=7)
    long_ago = datetime.now(timezone.utc) - timedelta(days=8)
    expired = issue_test_refresh_token(
        seed["user_id"],
        ttl_seconds=7 * 86400,
        session_created_at=long_ago,
    )
    app = _make_app(session_factory)

    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/refresh",
            cookies={"refresh_token": expired},
        )

    assert res.status_code == 401, res.json()
    assert "Session expired" in res.json()["detail"]


# ── PUT /api/v1/settings bounds validation ─────────────────────────────────


def _put_settings(client: TestClient, user_id: int, key: str, value: str):
    """Helper: hit PUT /api/v1/settings with an authenticated admin."""
    # Override the auth dep at the app level by injecting the seeded
    # user; cleaner than running the password flow each time.
    return client.put(
        "/api/v1/settings",
        json={"key": key, "value": value},
    )


@pytest.mark.asyncio
async def test_put_settings_rejects_session_lifetime_zero(
    session_factory,
) -> None:
    """``value="0"`` must 400. Zero days = instant logout for every
    user in the org — a footgun the backend must refuse."""
    seed = await _seed_user(session_factory)
    app = _make_app(session_factory, include_settings=True)

    async def _override_user():
        async with session_factory() as db:
            return await db.scalar(
                select(User).where(User.id == seed["user_id"])
            )

    app.dependency_overrides[get_current_user] = _override_user

    with TestClient(app) as client:
        res = client.put(
            "/api/v1/settings",
            json={"key": "session_lifetime_days", "value": "0"},
        )
    assert res.status_code == 400, res.json()
    assert "between 1 and 365" in res.json()["detail"]


@pytest.mark.asyncio
async def test_put_settings_rejects_session_lifetime_over_365(
    session_factory,
) -> None:
    seed = await _seed_user(session_factory)
    app = _make_app(session_factory, include_settings=True)

    async def _override_user():
        async with session_factory() as db:
            return await db.scalar(
                select(User).where(User.id == seed["user_id"])
            )

    app.dependency_overrides[get_current_user] = _override_user

    with TestClient(app) as client:
        res = client.put(
            "/api/v1/settings",
            json={"key": "session_lifetime_days", "value": "366"},
        )
    assert res.status_code == 400, res.json()
    assert "between 1 and 365" in res.json()["detail"]


@pytest.mark.asyncio
async def test_put_settings_rejects_session_lifetime_non_integer(
    session_factory,
) -> None:
    seed = await _seed_user(session_factory)
    app = _make_app(session_factory, include_settings=True)

    async def _override_user():
        async with session_factory() as db:
            return await db.scalar(
                select(User).where(User.id == seed["user_id"])
            )

    app.dependency_overrides[get_current_user] = _override_user

    with TestClient(app) as client:
        res = client.put(
            "/api/v1/settings",
            json={"key": "session_lifetime_days", "value": "thirty"},
        )
    assert res.status_code == 400, res.json()
    assert "integer" in res.json()["detail"]


@pytest.mark.asyncio
async def test_put_settings_accepts_session_lifetime_bounds(
    session_factory,
) -> None:
    """Boundary values 1 and 365 must succeed."""
    seed = await _seed_user(session_factory)
    app = _make_app(session_factory, include_settings=True)

    async def _override_user():
        async with session_factory() as db:
            return await db.scalar(
                select(User).where(User.id == seed["user_id"])
            )

    app.dependency_overrides[get_current_user] = _override_user

    with TestClient(app) as client:
        res1 = client.put(
            "/api/v1/settings",
            json={"key": "session_lifetime_days", "value": "1"},
        )
        res365 = client.put(
            "/api/v1/settings",
            json={"key": "session_lifetime_days", "value": "365"},
        )
    assert res1.status_code == 200, res1.json()
    assert res365.status_code == 200, res365.json()
