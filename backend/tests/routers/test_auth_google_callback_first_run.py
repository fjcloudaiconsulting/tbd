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
from sqlalchemy import func, select
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
    factory: async_sessionmaker[AsyncSession],
    *,
    email: str,
    is_superadmin: bool = False,
    is_active: bool = True,
) -> int:
    """Seed a pre-existing account.

    TBD-365: `is_superadmin` / `is_active` are load-bearing for the bootstrap
    fences below. `is_superadmin=False` builds the DIVERGENT state (users
    exist, none carries the flag) — the only state where the retired
    flag-count predicate and the empty-table predicate disagree.
    """
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
            is_superadmin=is_superadmin,
            is_active=is_active,
            email_verified=True,
        )
        db.add(user)
        await db.commit()
        return user.id


async def _counts(factory: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    """``(total users, superadmins)`` straight from the DB.

    TBD-365: the controls here must be DB-level for the same reason as in
    `tests/auth/test_register_login_bootstrap.py` — after the fix the
    divergent state has no observable consequence at the HTTP layer, so only
    the stored rows can prove the fixture built it and that the handler ran.
    """
    async with factory() as db:
        total = await db.scalar(select(func.count()).select_from(User))
        supers = await db.scalar(
            select(func.count()).select_from(User).where(User.is_superadmin.is_(True))
        )
    return int(total or 0), int(supers or 0)


def _drive_callback(client: TestClient) -> Any:
    """Drive the callback's happy path.

    ⚠ The 302 it returns is NOT a liveness signal — `_google_error_redirect`
    also returns 302. Every fence below proves liveness with the `_counts`
    assertions instead. Do not copy a bare `assert res.status_code == 302`
    into a new fence and treat it as proof the handler ran.
    """
    client.cookies.set("oauth_state", "matching-state")
    return client.get(
        "/api/v1/auth/google/callback",
        params={"code": "dummy", "state": "matching-state"},
        follow_redirects=False,
    )


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
    # TBD-365: exact equality is deliberate HERE (unlike the register file's
    # read-by-key posture) — this payload is the ticket's deliverable and is
    # now specified. Exact matching is what kills a stray `captcha_required`
    # (a gate this path never consults; see TBD-291 on poisoned denominators)
    # and a reintroduced `existing_superadmin_count`.
    #
    # Cold install here, so every first-ness value is True.
    assert row.detail == {
        "method": "google_sso",
        "is_first_user": True,
        "granted_superadmin": True,
        "email_verified_on_create": True,
    }
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


# ── TBD-365 bootstrap fences ────────────────────────────────────────────────
#
# Before TBD-365 the SSO grant was unfenced in BOTH directions: nothing
# asserted a cold install grants superadmin, and nothing asserted a warm one
# does not. That mattered because this is an unauthenticated public GET that
# mints users, has no captcha gate, and issues a session in the same redirect.


@pytest.mark.asyncio
async def test_tbd365_sso_divergent_state_grants_no_superadmin(
    session_factory, google_config, monkeypatch
) -> None:
    """FENCE — users exist, zero superadmins: a fresh Google sign-in gets nothing.

    Wrong implementation killed: `is_superadmin=(existing_superadmin == 0)` at
    the Google constructor — i.e. the pre-TBD-365 code, and the SHAPE OF THE
    MOST LIKELY PARTIAL FIX. Every existing test and the whole ticket
    narrative sit on the register side, so fixing `register` alone and leaving
    the callback on the flag count satisfies the register fences completely.
    Only this fence is red against that.

    The `existing_superadmin_count`-style control is the DB pre-condition: if
    the fixture accidentally seeded a superadmin, `granted_superadmin False`
    would pass for the boring reason the warm-install fence already covers.
    """
    await _seed_default_plan(session_factory)
    await _seed_existing_sso_user(
        session_factory, email="someone-else@example.com", is_superadmin=False
    )

    before = await _counts(session_factory)
    assert before == (1, 0), f"fixture did not build the divergent state: {before}"

    _patch_httpx(monkeypatch, userinfo_email="brand-new@example.com")
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = _drive_callback(client)
    assert res.status_code == 302, res.text

    after = await _counts(session_factory)
    assert after == (2, 0), (
        "the Google callback either did not create the user or minted a "
        f"superadmin outside the bootstrap. before={before} after={after}"
    )

    rows = await _audit_rows(
        session_factory, event_type="auth.google.callback.created_user"
    )
    assert len(rows) == 1
    assert rows[0].detail == {
        "method": "google_sso",
        "is_first_user": False,
        "granted_superadmin": False,
        "email_verified_on_create": True,
    }


@pytest.mark.asyncio
async def test_tbd365_sso_cold_install_still_bootstraps(
    session_factory, google_config, monkeypatch
) -> None:
    """FENCE — the cold-install SSO bootstrap survives.

    Wrong implementation killed: `is_superadmin=False` unconditional at the
    Google constructor — the lazy way to satisfy the divergent-state fence
    above, which would leave an install bootstrapped via Google with NO
    operator account. Nothing in the repo noticed this before TBD-365.

    Also kills reading the count AFTER `db.add(user)`/`flush`, which would see
    the row being inserted, return 1, and deny the first user their grant.
    """
    await _seed_default_plan(session_factory)
    assert await _counts(session_factory) == (0, 0)

    _patch_httpx(monkeypatch, userinfo_email="founder@example.com")
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = _drive_callback(client)
    assert res.status_code == 302, res.text

    assert await _counts(session_factory) == (1, 1), (
        "the first account on an empty install did not receive superadmin via "
        "Google SSO; the install has no operator"
    )


@pytest.mark.asyncio
async def test_tbd365_sso_warm_install_with_superadmin_grants_nothing(
    session_factory, google_config, monkeypatch
) -> None:
    """FENCE — a superadmin already exists: a new SSO signup gets nothing.

    ⚠ THIS FENCE HAS NO UNIQUE KILL, and says so rather than claiming one.

    An earlier draft claimed it killed "scoping the count to the new user's
    org". It cannot: the predicate is read BEFORE `_create_org_with_defaults`
    runs, so there is no org id to scope to at that point. And an org-scoped
    count would already be red on the divergent-state fence above, which sees
    `(2, 1)` instead of `(2, 0)`.

    It is also GREEN against the retired flag-count predicate, because a
    superadmin exists here so both predicates agree — the classic
    "fixture where right and wrong agree" shape.

    Kept as a plain behavioural assertion that the ordinary warm-install SSO
    path grants nothing, which is worth pinning for its own sake. Do NOT count
    it toward rekey coverage, and do not delete the divergent-state fence
    believing this one covers it.
    """
    await _seed_default_plan(session_factory)
    await _seed_existing_sso_user(
        session_factory, email="boss@example.com", is_superadmin=True
    )
    assert await _counts(session_factory) == (1, 1)

    _patch_httpx(monkeypatch, userinfo_email="brand-new@example.com")
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = _drive_callback(client)
    assert res.status_code == 302, res.text

    assert await _counts(session_factory) == (2, 1), (
        "a second account received superadmin while one already existed"
    )


@pytest.mark.asyncio
async def test_tbd365_deactivated_superadmin_does_not_rearm_the_bootstrap(
    session_factory, google_config, monkeypatch
) -> None:
    """FENCE — a SOFT-DELETED superadmin still closes the bootstrap.

    Wrong implementation killed: adding `User.is_active.is_(True)` to the
    bootstrap count. Explicitly rejected in the TBD-365 DoD, and it re-arms
    the exact hazard the ticket removed the instant any user is soft-deleted.

    Not theoretical: `remove_member` soft-deletes, and `admin_orgs_service`
    carries a large comment insisting its sibling count has NO `is_active`
    filter — a reader moving between those files can easily add one here
    believing it is the same question.
    """
    await _seed_default_plan(session_factory)
    await _seed_existing_sso_user(
        session_factory,
        email="retired-boss@example.com",
        is_superadmin=True,
        is_active=False,
    )
    assert await _counts(session_factory) == (1, 1)

    _patch_httpx(monkeypatch, userinfo_email="opportunist@example.com")
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = _drive_callback(client)
    assert res.status_code == 302, res.text

    assert await _counts(session_factory) == (2, 1), (
        "a soft-deleted superadmin re-armed the bootstrap and a public Google "
        "sign-in received the platform flag"
    )
