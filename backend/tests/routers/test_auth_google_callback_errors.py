"""Regression tests for the Google SSO callback "friendly error" path.

Pre-fix: every failure branch in ``/api/v1/auth/google/callback``
raised ``HTTPException(400)``. On DigitalOcean App Platform that 400
on a top-level browser GET navigation rendered the generic
"Error / check logs" splash, leaving users staring at a broken-app
screen instead of an actionable retry message.

Post-fix: each failure returns a ``RedirectResponse(307)`` to
``${app_url}/login?sso_error=<code>``. The frontend reads the
``sso_error`` query string and shows a friendly banner per code.

These tests pin:

  - ``state``     — missing or mismatched ``oauth_state`` cookie
  - ``token``     — token-exchange POST returns non-200 (or raises)
  - ``userinfo``  — userinfo GET returns non-200
  - ``unverified``— Google's ``verified_email`` flag is False
  - ``deactivated``— existing user with ``is_active=False``
  - ``no_email``  — Google returns no email

Each redirect also emits an ``auth.google.callback.failed`` audit
row with ``detail.reason`` set to the code, and clears the
``oauth_state`` cookie so a retry starts clean.

The SSO step-up callback has the same treatment, redirecting to
``${app_url}/settings?sso_stepup_error=state`` on the equivalent
state-cookie miss.
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
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.subscription import Plan
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers import auth as auth_module
from app.routers.auth import router as auth_router
from app.security import hash_password


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
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def google_config(monkeypatch):
    """Populate the Google OAuth env so ``_validate_google_config``
    passes. ``app_url`` is the origin the redirect Location will use,
    so we anchor it to ``http://localhost`` and assert on prefix."""
    monkeypatch.setattr(app_settings, "google_client_id", "test-client-id")
    monkeypatch.setattr(app_settings, "google_client_secret", "test-client-secret")
    monkeypatch.setattr(app_settings, "app_url", "http://localhost")
    yield


def _make_app(session_factory, current_user_id: int | None = None) -> FastAPI:
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_session_factory():
        # Wire both the request session and the independent audit-write
        # session at the same in-memory factory so audit rows the
        # callback emits land in the DB the test queries.
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


async def _seed_default_plan(factory: async_sessionmaker[AsyncSession]) -> None:
    """``create_trial`` (called on the new-user branch of the Google
    callback) needs at least one active plan. Seed the bare minimum so
    the success-path test doesn't 500 the way ``test_auth_email_dedupe``
    avoids the same trap."""
    async with factory() as db:
        existing = await db.scalar(select(Plan).where(Plan.slug == "free"))
        if existing is None:
            db.add(Plan(slug="free", name="Free", is_active=True, sort_order=0))
            await db.commit()


async def _seed_user(
    factory: async_sessionmaker[AsyncSession],
    *,
    email: str = "alice@acme.io",
    username: str = "alice",
    is_active: bool = True,
) -> int:
    async with factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id,
            username=username,
            email=email,
            password_hash=hash_password("starting-password-1"),
            role=Role.OWNER,
            is_active=is_active,
            email_verified=True,
        )
        db.add(user)
        await db.commit()
        return user.id


async def _callback_failure_rows(factory, *, event_type: str = "auth.google.callback.failed") -> list[AuditEvent]:
    async with factory() as db:
        result = await db.execute(
            select(AuditEvent).where(AuditEvent.event_type == event_type)
        )
        return list(result.scalars().all())


# ── helpers for the httpx mock ──────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict[str, Any]:
        return self._payload


def _patch_httpx(
    monkeypatch,
    *,
    token_status: int = 200,
    token_payload: dict[str, Any] | None = None,
    userinfo_status: int = 200,
    userinfo_payload: dict[str, Any] | None = None,
    raise_on_request: bool = False,
) -> None:
    """Replace ``auth_module.httpx.AsyncClient`` with a fake that
    returns the supplied canned responses (or raises ``httpx.HTTPError``
    on every call when ``raise_on_request`` is True)."""

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def post(self, *args: Any, **kwargs: Any) -> _FakeResponse:
            if raise_on_request:
                import httpx
                raise httpx.HTTPError("boom")
            return _FakeResponse(
                token_status, token_payload or {"access_token": "fake-token"}
            )

        async def get(self, *args: Any, **kwargs: Any) -> _FakeResponse:
            if raise_on_request:
                import httpx
                raise httpx.HTTPError("boom")
            return _FakeResponse(
                userinfo_status,
                userinfo_payload
                or {
                    "email": "alice@acme.io",
                    "verified_email": True,
                    "given_name": "Alice",
                    "family_name": "A",
                },
            )

    monkeypatch.setattr(auth_module.httpx, "AsyncClient", _FakeClient)


# ── /google/callback friendly error tests ───────────────────────────────────


@pytest.mark.asyncio
async def test_expired_oauth_state_cookie_redirects_with_state_code(
    session_factory, google_config
) -> None:
    """The production bug. User dwelt on Google's "Choose an account"
    dialog past the cookie TTL; on return the cookie was gone but the
    state query param was still there. Previously: 400 + DO error page.
    Now: 307 ``/login?sso_error=state`` so the LoginPageBody banner
    renders the right copy, plus an audit row."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        # Deliberately do NOT set the oauth_state cookie. This is the
        # "the cookie expired while the user was on Google" case.
        res = client.get(
            "/api/v1/auth/google/callback",
            params={"code": "dummy", "state": "some-state-value"},
            follow_redirects=False,
        )

    assert res.status_code == 307, res.text
    location = res.headers.get("location", "")
    assert location == "http://localhost/login?sso_error=state", location

    rows = await _callback_failure_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].outcome.value == "failure"
    assert rows[0].detail == {"reason": "state"}
    # No user identified at this stage of the flow.
    assert rows[0].actor_user_id is None
    assert rows[0].actor_email == ""


@pytest.mark.asyncio
async def test_token_exchange_failure_redirects_with_token_code(
    session_factory, google_config, monkeypatch
) -> None:
    """Google's /token endpoint returns 500 (transient outage). The
    callback should land the user back on /login with a retry-friendly
    banner, not on DO's generic error splash."""
    _patch_httpx(monkeypatch, token_status=500)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "matching-state")
        res = client.get(
            "/api/v1/auth/google/callback",
            params={"code": "dummy", "state": "matching-state"},
            follow_redirects=False,
        )

    assert res.status_code == 307, res.text
    assert res.headers.get("location") == "http://localhost/login?sso_error=token"

    rows = await _callback_failure_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].detail == {"reason": "token"}


@pytest.mark.asyncio
async def test_httpx_error_during_token_exchange_redirects_with_token_code(
    session_factory, google_config, monkeypatch
) -> None:
    """Network/DNS failure mid-request raises ``httpx.HTTPError``. The
    same friendly-error path applies — previously this surfaced as a
    502 and got wrapped by App Platform."""
    _patch_httpx(monkeypatch, raise_on_request=True)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "matching-state")
        res = client.get(
            "/api/v1/auth/google/callback",
            params={"code": "dummy", "state": "matching-state"},
            follow_redirects=False,
        )

    assert res.status_code == 307, res.text
    assert res.headers.get("location") == "http://localhost/login?sso_error=token"


@pytest.mark.asyncio
async def test_unverified_email_redirects_with_unverified_code(
    session_factory, google_config, monkeypatch
) -> None:
    """Google returns ``verified_email: False``. The user can recover
    by verifying their email with Google or signing in with a
    password; the banner must say so."""
    _patch_httpx(
        monkeypatch,
        userinfo_payload={
            "email": "unverified@example.com",
            "verified_email": False,
            "given_name": "U",
            "family_name": "V",
        },
    )

    app = _make_app(session_factory)
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "matching-state")
        res = client.get(
            "/api/v1/auth/google/callback",
            params={"code": "dummy", "state": "matching-state"},
            follow_redirects=False,
        )

    assert res.status_code == 307, res.text
    assert res.headers.get("location") == "http://localhost/login?sso_error=unverified"

    rows = await _callback_failure_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].detail == {"reason": "unverified"}
    # By this point we know the email Google reported (even though
    # unverified). Persist it on the audit row for ops triage.
    assert rows[0].actor_email == "unverified@example.com"


@pytest.mark.asyncio
async def test_deactivated_user_redirects_with_deactivated_code(
    session_factory, google_config, monkeypatch
) -> None:
    """Existing user with ``is_active=False``. The previous 403 raise
    became a redirect to ``/login?sso_error=deactivated`` so the user
    sees a real message instead of an error page."""
    await _seed_user(
        session_factory, email="deactivated@acme.io", is_active=False
    )
    _patch_httpx(
        monkeypatch,
        userinfo_payload={
            "email": "deactivated@acme.io",
            "verified_email": True,
            "given_name": "D",
            "family_name": "E",
        },
    )

    app = _make_app(session_factory)
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "matching-state")
        res = client.get(
            "/api/v1/auth/google/callback",
            params={"code": "dummy", "state": "matching-state"},
            follow_redirects=False,
        )

    assert res.status_code == 307, res.text
    assert (
        res.headers.get("location")
        == "http://localhost/login?sso_error=deactivated"
    )

    rows = await _callback_failure_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].detail == {"reason": "deactivated"}


@pytest.mark.asyncio
async def test_userinfo_failure_redirects_with_userinfo_code(
    session_factory, google_config, monkeypatch
) -> None:
    """Google's /userinfo endpoint returns 500 after token exchange
    succeeded. Same friendly-error treatment as ``token``."""
    _patch_httpx(monkeypatch, userinfo_status=500)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "matching-state")
        res = client.get(
            "/api/v1/auth/google/callback",
            params={"code": "dummy", "state": "matching-state"},
            follow_redirects=False,
        )

    assert res.status_code == 307, res.text
    assert (
        res.headers.get("location") == "http://localhost/login?sso_error=userinfo"
    )


@pytest.mark.asyncio
async def test_no_email_from_google_redirects_with_no_email_code(
    session_factory, google_config, monkeypatch
) -> None:
    """Google omits an email from the userinfo payload (an edge case
    seen with restricted scopes). Pre-fix: 400. Post-fix: friendly
    redirect with the ``no_email`` banner copy."""
    _patch_httpx(
        monkeypatch,
        userinfo_payload={
            "verified_email": True,
            "given_name": "N",
            "family_name": "E",
        },
    )

    app = _make_app(session_factory)
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "matching-state")
        res = client.get(
            "/api/v1/auth/google/callback",
            params={"code": "dummy", "state": "matching-state"},
            follow_redirects=False,
        )

    assert res.status_code == 307, res.text
    assert (
        res.headers.get("location") == "http://localhost/login?sso_error=no_email"
    )


@pytest.mark.asyncio
async def test_successful_google_callback_still_redirects_to_frontend(
    session_factory, google_config, monkeypatch
) -> None:
    """Sanity check — the redirect-on-error refactor did not break the
    success path. A matching state cookie + verified email + healthy
    Google responses still produces a 302 to the frontend
    /auth/google/callback#token=... URL."""
    await _seed_default_plan(session_factory)
    _patch_httpx(monkeypatch)

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

    # And no failure audit row should have landed.
    rows = await _callback_failure_rows(session_factory)
    assert rows == []


# ── /sso-stepup/callback friendly error tests ───────────────────────────────


@pytest.mark.asyncio
async def test_stepup_expired_oauth_state_cookie_redirects_with_state_code(
    session_factory, google_config
) -> None:
    """Same DO-error-page problem on the step-up flow. An expired
    cookie + lingering state query param now resolves to a 307
    redirect to ``/settings?sso_stepup_error=state`` so the
    settings page can render a friendly banner."""
    user_id = await _seed_user(session_factory)
    app = _make_app(session_factory, user_id)
    with TestClient(app) as client:
        res = client.get(
            "/api/v1/auth/sso-stepup/callback",
            params={"code": "dummy", "state": f"stepup:{user_id}:nonce:settings"},
            follow_redirects=False,
        )

    assert res.status_code == 307, res.text
    location = res.headers.get("location", "")
    assert location.endswith("/settings?sso_stepup_error=state"), location

    rows = await _callback_failure_rows(
        session_factory, event_type="auth.google.sso_stepup.callback.failed"
    )
    assert len(rows) == 1
    assert rows[0].detail == {"reason": "state"}


# ── cookie TTL pin ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_google_login_sets_oauth_state_cookie_for_30_minutes(
    session_factory, google_config
) -> None:
    """Pin the cookie TTL bump from 600 (10 min) to 1800 (30 min).
    The 10-min budget proved too tight in production: users dwelt
    ~11 min on Google's account picker, the cookie expired, and the
    callback CSRF check failed."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.get("/api/v1/auth/google")
    assert res.status_code == 200
    set_cookie = res.headers.get("set-cookie", "")
    # set-cookie header carries Max-Age=1800
    assert "Max-Age=1800" in set_cookie, set_cookie
    assert "oauth_state=" in set_cookie
