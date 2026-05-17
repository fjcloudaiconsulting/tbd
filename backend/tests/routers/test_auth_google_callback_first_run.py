"""Regression tests for the first-run SSO disclosure signal.

The Google SSO callback distinguishes the "create new local user"
branch from the "log in existing local user" branch. To let the
frontend show the privacy disclosure step only to genuinely fresh
SSO users, we surface that distinction two ways:

  - **Audit event split**: the new-user branch writes a dedicated
    ``auth.google.callback.created_user`` row in addition to the
    existing ``user.login.success`` row. The existing-user branch
    keeps emitting only ``user.login.success``.
  - **Redirect-fragment signal**: the redirect URL the callback
    returns gets ``&created_user=true`` appended AFTER the token in
    the URL fragment (never the query string). This rides on the
    same fragment-only privacy posture as the token itself, so the
    flag is not surfaced in Referer headers or server logs.

The tests below pin both signals against a real first-run callback
and a real returning-user callback.
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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_session_factory
from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.subscription import Plan
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers import auth as auth_module
from app.routers.auth import router as auth_router
from app.security import hash_password


# ── fixtures (mirror test_auth_google_callback_errors) ──────────────────────


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


@pytest.fixture
def google_config(monkeypatch):
    monkeypatch.setattr(app_settings, "google_client_id", "test-client-id")
    monkeypatch.setattr(app_settings, "google_client_secret", "test-client-secret")
    monkeypatch.setattr(app_settings, "app_url", "http://localhost")
    yield


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


async def _seed_default_plan(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as db:
        existing = await db.scalar(select(Plan).where(Plan.slug == "free"))
        if existing is None:
            db.add(Plan(slug="free", name="Free", is_active=True, sort_order=0))
            await db.commit()


async def _seed_existing_sso_user(
    factory: async_sessionmaker[AsyncSession], *, email: str
) -> int:
    async with factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id,
            username="returning",
            email=email,
            password_hash=hash_password("starting-password-1"),
            role=Role.OWNER,
            is_active=True,
            email_verified=True,
        )
        db.add(user)
        await db.commit()
        return user.id


async def _audit_rows(
    factory: async_sessionmaker[AsyncSession], *, event_type: str
) -> list[AuditEvent]:
    async with factory() as db:
        result = await db.execute(
            select(AuditEvent).where(AuditEvent.event_type == event_type)
        )
        return list(result.scalars().all())


# ── httpx mock ──────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict[str, Any]:
        return self._payload


def _patch_httpx(
    monkeypatch,
    *,
    userinfo_email: str,
) -> None:
    """Mock httpx so the Google /token and /userinfo calls return a
    valid token payload + a verified userinfo payload for the given
    email. Used by both the new-user and existing-user tests."""

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
                    "given_name": "First",
                    "family_name": "Last",
                },
            )

    monkeypatch.setattr(auth_module.httpx, "AsyncClient", _FakeClient)


# ── new-user branch ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_new_sso_user_redirect_url_carries_created_user_flag(
    session_factory, google_config, monkeypatch
) -> None:
    """Brand-new email at the Google callback creates a local user and
    redirects to ``/auth/google/callback#token=...&created_user=true``.
    The flag rides on the FRAGMENT (not the query string) so it never
    appears in Referer headers or server access logs.
    """
    await _seed_default_plan(session_factory)
    _patch_httpx(monkeypatch, userinfo_email="brand-new@example.com")

    app = _make_app(session_factory)
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "matching-state")
        res = client.get(
            "/api/v1/auth/google/callback",
            params={"code": "dummy", "state": "matching-state"},
            follow_redirects=False,
        )

    assert res.status_code == 302, res.text
    location = res.headers.get("location", "")
    # Fragment carries both token and created_user, with no leakage
    # into the query string.
    assert "#token=" in location, location
    assert "&created_user=true" in location, location
    # ``?created_user`` in the query string would be a privacy bug —
    # query params land in Referer headers + server logs.
    pre_fragment = location.split("#", 1)[0]
    assert "created_user" not in pre_fragment, pre_fragment
    assert "?" not in pre_fragment.split("/callback", 1)[1], pre_fragment


@pytest.mark.asyncio
async def test_new_sso_user_records_created_user_audit_event(
    session_factory, google_config, monkeypatch
) -> None:
    """The new-user branch writes ``auth.google.callback.created_user``
    in addition to the existing ``user.login.success`` row. Ops can
    filter the audit log on the dedicated event for the first-run
    slice without breaking existing login analytics.
    """
    await _seed_default_plan(session_factory)
    _patch_httpx(monkeypatch, userinfo_email="brand-new@example.com")

    app = _make_app(session_factory)
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "matching-state")
        res = client.get(
            "/api/v1/auth/google/callback",
            params={"code": "dummy", "state": "matching-state"},
            follow_redirects=False,
        )
    assert res.status_code == 302, res.text

    created_rows = await _audit_rows(
        session_factory, event_type="auth.google.callback.created_user"
    )
    assert len(created_rows) == 1
    row = created_rows[0]
    assert row.outcome.value == "success"
    assert row.actor_email == "brand-new@example.com"
    assert row.detail == {"method": "google_sso"}
    # The new-user branch still emits the standard login event.
    login_rows = await _audit_rows(session_factory, event_type="user.login.success")
    assert len(login_rows) == 1
    assert login_rows[0].detail == {"method": "google_sso"}


@pytest.mark.asyncio
async def test_new_sso_user_audit_detail_carries_no_token_or_secret(
    session_factory, google_config, monkeypatch
) -> None:
    """Defence in depth: the created_user audit row must not carry
    any token / secret value in its detail dict, and the redirect
    Location must not surface the token in the query string."""
    await _seed_default_plan(session_factory)
    _patch_httpx(monkeypatch, userinfo_email="brand-new@example.com")

    app = _make_app(session_factory)
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "matching-state")
        res = client.get(
            "/api/v1/auth/google/callback",
            params={"code": "dummy", "state": "matching-state"},
            follow_redirects=False,
        )
    assert res.status_code == 302, res.text

    created_rows = await _audit_rows(
        session_factory, event_type="auth.google.callback.created_user"
    )
    assert len(created_rows) == 1
    detail = created_rows[0].detail or {}
    assert "token" not in detail
    assert "access_token" not in detail
    assert "refresh_token" not in detail
    # Sanity: redirect URL pre-fragment never carries the token either.
    location = res.headers.get("location", "")
    pre_fragment = location.split("#", 1)[0]
    assert "token=" not in pre_fragment
    # And the refresh cookie stays HttpOnly + SameSite=lax. The
    # set-cookie header may carry multiple cookies (the new refresh,
    # the deleted oauth_state, and the deleted legacy refresh). Use
    # the structured cookie jar to read the live refresh_token rather
    # than parsing the raw multi-cookie header.
    refresh_value = res.cookies.get("refresh_token")
    assert refresh_value, dict(res.cookies)
    raw_cookies_lower = res.headers.get("set-cookie", "").lower()
    assert "httponly" in raw_cookies_lower
    assert "samesite=lax" in raw_cookies_lower


# ── existing-user branch (regression pin) ───────────────────────────────────


@pytest.mark.asyncio
async def test_existing_sso_user_redirect_url_has_no_created_user_flag(
    session_factory, google_config, monkeypatch
) -> None:
    """Returning SSO users must NOT see the disclosure. The redirect
    URL therefore must not carry ``created_user`` anywhere.
    """
    await _seed_default_plan(session_factory)
    await _seed_existing_sso_user(session_factory, email="returning@example.com")
    _patch_httpx(monkeypatch, userinfo_email="returning@example.com")

    app = _make_app(session_factory)
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "matching-state")
        res = client.get(
            "/api/v1/auth/google/callback",
            params={"code": "dummy", "state": "matching-state"},
            follow_redirects=False,
        )

    assert res.status_code == 302, res.text
    location = res.headers.get("location", "")
    assert location.startswith("http://localhost/auth/google/callback#token="), location
    assert "created_user" not in location, location


@pytest.mark.asyncio
async def test_existing_sso_user_does_not_record_created_user_audit(
    session_factory, google_config, monkeypatch
) -> None:
    """Returning SSO users keep emitting only ``user.login.success`` —
    the dedicated created_user event stays specific to the new-user
    branch."""
    await _seed_default_plan(session_factory)
    await _seed_existing_sso_user(session_factory, email="returning@example.com")
    _patch_httpx(monkeypatch, userinfo_email="returning@example.com")

    app = _make_app(session_factory)
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "matching-state")
        res = client.get(
            "/api/v1/auth/google/callback",
            params={"code": "dummy", "state": "matching-state"},
            follow_redirects=False,
        )
    assert res.status_code == 302, res.text

    created_rows = await _audit_rows(
        session_factory, event_type="auth.google.callback.created_user"
    )
    assert created_rows == []
    login_rows = await _audit_rows(session_factory, event_type="user.login.success")
    assert len(login_rows) == 1
    assert login_rows[0].detail == {"method": "google_sso"}
