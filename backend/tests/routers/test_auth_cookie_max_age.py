"""Pin the refresh-cookie ``Max-Age`` attribute at every entry point.

After PR 1 of the backend-session-model rollout
(``specs/2026-05-17-backend-session-model.md``) every site that issues
the refresh cookie reads its ``Max-Age`` from a single helper
(``_refresh_cookie_max_age``) backed by ``REFRESH_IDLE_TTL_DAYS``. The
spec's AC1 ("Single cookie TTL source of truth") says: changing
``REFRESH_IDLE_TTL_DAYS`` and restarting the backend must change the
``Max-Age`` attribute at every entry point in lockstep.

This file pins four entry points:

  - ``POST /api/v1/auth/login``  (password branch)
  - ``POST /api/v1/auth/refresh``  (rotation)
  - ``POST /api/v1/auth/mfa/recovery``  (MFA branch via ``_issue_tokens``)
  - ``GET  /api/v1/auth/google/callback``  (SSO redirect)

For each, we assert ``Max-Age`` matches ``app_settings.refresh_idle_ttl_days
* 86400``. The fourth test monkey-patches the setting to 14 days and
re-asserts ALL four sites move to ``Max-Age=1209600`` together — the
canonical "no drift" pin for AC1.
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
from sqlalchemy import select
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
from app.models.subscription import Plan
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers import auth as auth_module
from app.routers.auth import LEGACY_REFRESH_COOKIE_PATH, router as auth_router
from app.security import (
    create_mfa_challenge_token,
    create_refresh_token,
    hash_password,
)
from app.services.mfa_service import (
    generate_recovery_codes,
    hash_recovery_code,
)


PASSWORD = "starting-password-1"


# ── fixtures ────────────────────────────────────────────────────────────────


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
    """SlowAPI Limiter is a module-level singleton; reset between tests
    so the per-IP counter doesn't leak."""
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


async def _seed_user(
    factory: async_sessionmaker[AsyncSession],
    *,
    mfa_enabled: bool = False,
    recovery_codes_plaintext: list[str] | None = None,
) -> dict:
    async with factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        recovery_field: str | None = None
        if recovery_codes_plaintext is not None:
            recovery_field = ",".join(
                hash_recovery_code(c) for c in recovery_codes_plaintext
            )
        user = User(
            org_id=org.id,
            username="alice",
            email="alice@example.com",
            password_hash=hash_password(PASSWORD),
            role=Role.OWNER,
            is_superadmin=False,
            is_active=True,
            email_verified=True,
            mfa_enabled=mfa_enabled,
            recovery_codes=recovery_field,
        )
        db.add(user)
        await db.commit()
        return {"org_id": org.id, "user_id": user.id}


async def _seed_default_plan(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as db:
        existing = await db.scalar(select(Plan).where(Plan.slug == "free"))
        if existing is None:
            db.add(Plan(slug="free", name="Free", is_active=True, sort_order=0))
            await db.commit()


# ── Set-Cookie parsing helpers ──────────────────────────────────────────────


def _set_cookie_values_for(headers, name: str) -> list[str]:
    """Return every Set-Cookie header whose cookie name is ``name``."""
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
    """Return the Set-Cookie value that ISSUES (not deletes) the
    canonical Path=/ refresh_token. Skips the legacy-path delete cookie."""
    cookies = _set_cookie_values_for(headers, "refresh_token")
    canonical = [
        c
        for c in cookies
        if "Path=/" in c
        and f"Path={LEGACY_REFRESH_COOKIE_PATH}" not in c
        # Exclude deletes (Max-Age=0).
        and "Max-Age=0" not in c
    ]
    assert canonical, (
        f"Expected one canonical Path=/ refresh_token Set-Cookie. "
        f"Got refresh_token Set-Cookies: {cookies}"
    )
    return canonical[0]


def _max_age_from_set_cookie(raw: str) -> int:
    """Parse the ``Max-Age=<n>`` attribute from a Set-Cookie header."""
    for part in raw.split(";"):
        part = part.strip()
        if part.lower().startswith("max-age="):
            return int(part.split("=", 1)[1])
    raise AssertionError(f"Set-Cookie has no Max-Age attribute: {raw!r}")


# ── httpx mock for the Google SSO callback ──────────────────────────────────


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
                    "given_name": "Existing",
                    "family_name": "User",
                },
            )

    monkeypatch.setattr(auth_module.httpx, "AsyncClient", _FakeClient)


@pytest.fixture
def google_config(monkeypatch):
    monkeypatch.setattr(app_settings, "google_client_id", "test-client-id")
    monkeypatch.setattr(app_settings, "google_client_secret", "test-client-secret")
    monkeypatch.setattr(app_settings, "app_url", "http://localhost")
    yield


# ── Default-TTL per-site pins (30 days = 2592000 seconds) ───────────────────


@pytest.mark.asyncio
async def test_login_password_cookie_max_age_matches_settings(
    session_factory,
) -> None:
    """``/auth/login`` (password branch) sets ``Max-Age`` equal to
    ``refresh_idle_ttl_days * 86400``."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)

    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": PASSWORD},
        )

    assert res.status_code == 200, res.json()
    raw = _canonical_refresh_cookie(res.headers)
    assert _max_age_from_set_cookie(raw) == app_settings.refresh_idle_ttl_days * 86400


@pytest.mark.asyncio
async def test_refresh_rotation_cookie_max_age_matches_settings(
    session_factory,
) -> None:
    """``/auth/refresh`` rotation sets ``Max-Age`` equal to
    ``refresh_idle_ttl_days * 86400``."""
    seed = await _seed_user(session_factory)
    refresh = create_refresh_token(seed["user_id"])
    app = _make_app(session_factory)

    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/refresh",
            cookies={"refresh_token": refresh},
        )

    assert res.status_code == 200, res.json()
    raw = _canonical_refresh_cookie(res.headers)
    assert _max_age_from_set_cookie(raw) == app_settings.refresh_idle_ttl_days * 86400


@pytest.mark.asyncio
async def test_mfa_recovery_cookie_max_age_matches_settings(
    session_factory,
) -> None:
    """``/auth/mfa/recovery`` issues the refresh cookie via the shared
    ``_issue_tokens`` helper; its ``Max-Age`` must match the config too."""
    codes = generate_recovery_codes(count=3)
    seed = await _seed_user(
        session_factory,
        mfa_enabled=True,
        recovery_codes_plaintext=codes,
    )
    mfa_token = create_mfa_challenge_token(seed["user_id"])
    app = _make_app(session_factory)

    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/mfa/recovery",
            json={"mfa_token": mfa_token, "code": codes[0]},
        )

    assert res.status_code == 200, res.json()
    raw = _canonical_refresh_cookie(res.headers)
    assert _max_age_from_set_cookie(raw) == app_settings.refresh_idle_ttl_days * 86400


@pytest.mark.asyncio
async def test_google_callback_cookie_max_age_matches_settings(
    session_factory, google_config, monkeypatch
) -> None:
    """``/auth/google/callback`` returns a 302 + Set-Cookie; ``Max-Age``
    must match the configured idle TTL."""
    await _seed_default_plan(session_factory)
    _patch_httpx(monkeypatch, userinfo_email="brand-new-sso@example.com")
    app = _make_app(session_factory)

    with TestClient(app) as client:
        client.cookies.set("oauth_state", "matching-state")
        res = client.get(
            "/api/v1/auth/google/callback",
            params={"code": "dummy", "state": "matching-state"},
            follow_redirects=False,
        )

    assert res.status_code == 302, res.text
    raw = _canonical_refresh_cookie(res.headers)
    assert _max_age_from_set_cookie(raw) == app_settings.refresh_idle_ttl_days * 86400


# ── AC1: all four sites move in lockstep when the setting changes ───────────


@pytest.mark.asyncio
async def test_all_four_sites_emit_same_max_age_when_setting_changes(
    session_factory, google_config, monkeypatch
) -> None:
    """The architect-locked acceptance for AC1 of the spec: changing
    ``REFRESH_IDLE_TTL_DAYS`` must move every issue site's ``Max-Age``
    in lockstep. We monkey-patch the setting to 14 days and assert all
    four entry points emit ``Max-Age=1209600`` (14 * 86400).

    The point of this test is structural: it would fail loudly if a
    future refactor reintroduces a hardcoded literal at any cookie
    issue site, or forgets to route a new site through
    ``_refresh_cookie_max_age``.
    """
    monkeypatch.setattr(app_settings, "refresh_idle_ttl_days", 14)
    expected = 14 * 86400  # 1_209_600

    codes = generate_recovery_codes(count=3)
    seed = await _seed_user(
        session_factory,
        mfa_enabled=False,
        recovery_codes_plaintext=None,
    )
    await _seed_default_plan(session_factory)

    app = _make_app(session_factory)

    # 1. Login (password branch).
    with TestClient(app) as client:
        login_res = client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": PASSWORD},
        )
    assert login_res.status_code == 200, login_res.json()
    login_raw = _canonical_refresh_cookie(login_res.headers)
    assert _max_age_from_set_cookie(login_raw) == expected, login_raw

    # 2. Refresh rotation.
    refresh = create_refresh_token(seed["user_id"])
    with TestClient(app) as client:
        refresh_res = client.post(
            "/api/v1/auth/refresh",
            cookies={"refresh_token": refresh},
        )
    assert refresh_res.status_code == 200, refresh_res.json()
    refresh_raw = _canonical_refresh_cookie(refresh_res.headers)
    assert _max_age_from_set_cookie(refresh_raw) == expected, refresh_raw

    # 3. MFA recovery branch (uses _issue_tokens). Seed an MFA-enabled
    #    user separately so the login above (password branch) wasn't
    #    short-circuited into the MFA challenge response.
    async with session_factory() as db:
        result = await db.execute(select(User).where(User.id == seed["user_id"]))
        user = result.scalar_one()
        user.mfa_enabled = True
        user.recovery_codes = ",".join(hash_recovery_code(c) for c in codes)
        await db.commit()
    mfa_token = create_mfa_challenge_token(seed["user_id"])
    with TestClient(app) as client:
        mfa_res = client.post(
            "/api/v1/auth/mfa/recovery",
            json={"mfa_token": mfa_token, "code": codes[0]},
        )
    assert mfa_res.status_code == 200, mfa_res.json()
    mfa_raw = _canonical_refresh_cookie(mfa_res.headers)
    assert _max_age_from_set_cookie(mfa_raw) == expected, mfa_raw

    # 4. Google SSO callback.
    _patch_httpx(monkeypatch, userinfo_email="brand-new-sso@example.com")
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "matching-state")
        sso_res = client.get(
            "/api/v1/auth/google/callback",
            params={"code": "dummy", "state": "matching-state"},
            follow_redirects=False,
        )
    assert sso_res.status_code == 302, sso_res.text
    sso_raw = _canonical_refresh_cookie(sso_res.headers)
    assert _max_age_from_set_cookie(sso_raw) == expected, sso_raw


# ── Invite accept site (architect feedback on PR #305) ─────────────────────


@pytest.mark.asyncio
async def test_invite_accept_cookie_max_age_matches_settings(
    session_factory, monkeypatch
) -> None:
    """Architect feedback on PR #305: the invitation-accept handler in
    ``backend/app/routers/org_members.py`` is a fifth refresh-cookie
    issue site that the original PR missed. After this fix it must
    move in lockstep with ``REFRESH_IDLE_TTL_DAYS`` like the other
    four sites in ``auth.py``. This test pins the contract by
    monkey-patching the setting and exercising the real endpoint
    end-to-end against the in-memory SQLite fixture.

    Without this regression, a future refactor could silently
    reintroduce a hardcoded ``max_age=7*24*60*60`` on the invite
    accept path and tests would not notice.
    """
    from app.routers.org_members import router as org_members_router
    from app.security import create_invitation_token
    from app.services import invitation_service

    monkeypatch.setattr(app_settings, "refresh_idle_ttl_days", 14)
    expected = 14 * 86400

    # Seed: org + owner so we can create an invitation off the owner.
    async with session_factory() as db:
        org = Organization(name="Inv Co", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        owner = User(
            org_id=org.id,
            username="owner",
            email="owner@inv.io",
            password_hash=hash_password(PASSWORD),
            role=Role.OWNER,
            is_superadmin=False,
            is_active=True,
            email_verified=True,
        )
        db.add(owner)
        await db.commit()
        org_id, owner_id = org.id, owner.id

    async with session_factory() as db:
        inv = await invitation_service.create_invitation(
            db,
            org_id=org_id,
            created_by=owner_id,
            email="invitee@inv.io",
            role=Role.MEMBER,
        )
        await db.commit()
        token = create_invitation_token(inv.id, inv.email)

    # Build a minimal app that mounts the org_members router and shares
    # the same SQLite session factory.
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.include_router(org_members_router)

    with TestClient(app) as client:
        res = client.post(
            "/api/v1/orgs/invitations/accept",
            json={
                "token": token,
                "username": "invitee",
                "password": "strong-pw-1234",
            },
        )
    assert res.status_code == 200, res.text
    raw = _canonical_refresh_cookie(res.headers)
    assert _max_age_from_set_cookie(raw) == expected, raw


def test_no_hardcoded_seven_day_refresh_cookie_literals_remain() -> None:
    """Grep guard pinning that no ``max_age=7 * 24 * 60 * 60`` literal
    remains anywhere in ``backend/app/`` after PR #305 + the architect's
    org_members.py patch. If a future PR reintroduces the pattern (or
    any other near-equivalent like ``604800`` written inline next to a
    refresh-cookie ``set_cookie``), this test fails loudly.

    Scope is the app source tree only; test fixtures may legitimately
    carry literals as expected values.
    """
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[2] / "app"
    offenders: list[str] = []
    # Two equivalent spellings of the old literal. The architect named
    # the spaced form; the unspaced form is what an over-eager linter
    # might rewrite it to.
    needles = ("7 * 24 * 60 * 60", "7*24*60*60")
    for py in app_dir.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for needle in needles:
            if needle in text:
                offenders.append(f"{py.relative_to(app_dir.parent)}: contains {needle!r}")
    assert offenders == [], (
        "Hardcoded 7-day refresh-cookie literals must be replaced by "
        "refresh_cookie_max_age() (see specs/2026-05-17-backend-session-model.md "
        "§5.4). Offenders: " + "; ".join(offenders)
    )


# ── Settings validator ──────────────────────────────────────────────────────


def test_refresh_idle_ttl_days_validator_rejects_zero() -> None:
    """``REFRESH_IDLE_TTL_DAYS=0`` must refuse to boot."""
    from app.config import Settings

    with pytest.raises(ValueError):
        Settings(
            jwt_secret_key="x" * 64,
            refresh_idle_ttl_days=0,
        )


def test_refresh_idle_ttl_days_validator_rejects_too_large() -> None:
    """``REFRESH_IDLE_TTL_DAYS=366`` must refuse to boot."""
    from app.config import Settings

    with pytest.raises(ValueError):
        Settings(
            jwt_secret_key="x" * 64,
            refresh_idle_ttl_days=366,
        )


def test_refresh_idle_ttl_days_validator_accepts_bounds() -> None:
    """Boundary values (1 and 365) must validate cleanly."""
    from app.config import Settings

    Settings(jwt_secret_key="x" * 64, refresh_idle_ttl_days=1)
    Settings(jwt_secret_key="x" * 64, refresh_idle_ttl_days=365)
