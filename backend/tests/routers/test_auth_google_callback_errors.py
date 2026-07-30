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

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

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
    raise_exc: BaseException | None = None,
    hang_on: str | None = None,
    delay_s: float = 0.0,
) -> None:
    """Replace ``auth_module.httpx.AsyncClient`` with a fake that
    returns the supplied canned responses (or raises ``httpx.HTTPError``
    on every call when ``raise_on_request`` is True).

    ``hang_on`` (``"post"``, ``"get"`` or ``"both"``) makes the named
    call sleep ``delay_s`` before answering, which is how the aggregate
    timeout tests drive the bound. The sleep is ``await
    asyncio.sleep(...)`` and never ``time.sleep(...)``: the fake runs on
    the TestClient's event loop, so a blocking sleep would wedge it.
    Tests keep themselves fast by monkeypatching
    ``auth_module.GOOGLE_OAUTH_TOTAL_TIMEOUT_S`` down, not by waiting.

    ``raise_exc`` raises an arbitrary exception from the token POST.
    That is deliberately distinct from ``raise_on_request`` so a test
    can prove the handler's ``except`` clauses do *not* swallow a
    programmer error.
    """

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def post(self, *args: Any, **kwargs: Any) -> _FakeResponse:
            if hang_on in ("post", "both"):
                await asyncio.sleep(delay_s)
            if raise_exc is not None:
                raise raise_exc
            if raise_on_request:
                import httpx
                raise httpx.HTTPError("boom")
            return _FakeResponse(
                token_status, token_payload or {"access_token": "fake-token"}
            )

        async def get(self, *args: Any, **kwargs: Any) -> _FakeResponse:
            if hang_on in ("get", "both"):
                await asyncio.sleep(delay_s)
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


# ── the aggregate-timeout constant ──────────────────────────────────────────


def test_aggregate_timeout_stays_above_the_per_phase_sum() -> None:
    """Pin the derivation of ``GOOGLE_OAUTH_TOTAL_TIMEOUT_S``.

    The aggregate bound covers two sequential HTTP calls, each of which
    is separately allowed a full per-phase read budget. If the aggregate
    is ever tightened below that sum — or the per-phase value raised
    without raising the aggregate — healthy exchanges start tripping the
    bound and every one of them surfaces to the user as
    ``?sso_error=token``. Nothing else in the suite notices: the timeout
    path has its own passing tests and the success-path tests use a fake
    client that answers instantly.
    """
    assert (
        auth_module.GOOGLE_OAUTH_TOTAL_TIMEOUT_S
        >= 2 * auth_module.GOOGLE_OAUTH_TIMEOUT.read
    )


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
    502 and got wrapped by App Platform.

    The audit assertions also fence the boundary with the aggregate
    timeout clause: httpx's own timeout family (``ConnectTimeout``,
    ``ReadTimeout``, ...) descends from ``HTTPError`` and *not* from
    builtin ``TimeoutError``, so it must keep landing here with
    ``reason: "token"``. An implementation that catches both in one
    tuple, orders the clauses so the timeout branch steals httpx
    errors, or drops this branch's audit write while refactoring
    around it, fails here.
    """
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

    rows = await _callback_failure_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].detail == {"reason": "token"}


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
    # First-run signal is appended after the token in the fragment
    # on the new-user branch (see test_auth_google_callback_first_run.py
    # for the full pin). The token-in-fragment shape is unchanged.
    assert location.startswith("http://localhost/auth/google/callback#token="), location

    # And no failure audit row should have landed.
    rows = await _callback_failure_rows(session_factory)
    assert rows == []


# ── aggregate exchange timeout ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_token_post_exceeding_the_aggregate_budget_redirects_and_audits_timeout(
    session_factory, google_config, monkeypatch
) -> None:
    """L1. Google accepts the connection and then never answers the
    token POST.

    Per-phase ``GOOGLE_OAUTH_TIMEOUT`` does not save us here: each of
    connect/write/read gets its own budget and ``read`` renews per
    socket read, so a drip-feeding provider holds the request open
    indefinitely. The aggregate bound must cut it, audit the distinct
    ``timeout`` reason (so ops can tell a wedged provider apart from a
    rejected code) and still hand the user the existing ``token``
    banner copy.

    The elapsed assertion is what fences "no bound at all": the fake
    sleeps 2s against a 0.05s budget, so an unbounded handler blows the
    1.0s ceiling as well as the Location assertion.

    The plan and the active user are seeded so that an implementation
    without the bound reaches a *clean* success redirect. Without them
    it would blow up on unrelated trial-creation setup, and this test
    would be "red" for a reason that has nothing to do with the bound.
    """
    await _seed_default_plan(session_factory)
    await _seed_user(session_factory, email="alice@acme.io")
    monkeypatch.setattr(auth_module, "GOOGLE_OAUTH_TOTAL_TIMEOUT_S", 0.05)
    _patch_httpx(monkeypatch, hang_on="post", delay_s=2.0)

    app = _make_app(session_factory)
    started = time.monotonic()
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "matching-state")
        # raise_server_exceptions defaults True, so an escaping
        # TimeoutError raises out of this call rather than returning
        # 500. That is the intended failure mode — never assert on a
        # status code that would not be reached.
        res = client.get(
            "/api/v1/auth/google/callback",
            params={"code": "dummy", "state": "matching-state"},
            follow_redirects=False,
        )
    elapsed = time.monotonic() - started

    assert res.status_code == 307, res.text
    assert res.headers.get("location") == "http://localhost/login?sso_error=token"
    assert elapsed < 1.0, elapsed

    rows = await _callback_failure_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].detail["reason"] == "timeout"


@pytest.mark.asyncio
async def test_userinfo_get_exceeding_the_aggregate_budget_records_last_phase(
    session_factory, google_config, monkeypatch
) -> None:
    """L2. The token POST lands and the userinfo GET is the wedged leg.

    A bound wrapping only the token POST passes L1 and fails here. The
    ``last_phase`` marker is the other half: without it a production
    timeout tells an operator nothing about *which* Google endpoint is
    stuck, which is the entire diagnostic value of the audit row.

    Plan + active user seeded for the same reason as L1: an unbounded
    implementation must fail this test on the redirect, not on
    unrelated trial-creation setup.
    """
    await _seed_default_plan(session_factory)
    await _seed_user(session_factory, email="alice@acme.io")
    monkeypatch.setattr(auth_module, "GOOGLE_OAUTH_TOTAL_TIMEOUT_S", 0.05)
    _patch_httpx(monkeypatch, hang_on="get", delay_s=2.0)

    app = _make_app(session_factory)
    started = time.monotonic()
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "matching-state")
        res = client.get(
            "/api/v1/auth/google/callback",
            params={"code": "dummy", "state": "matching-state"},
            follow_redirects=False,
        )
    elapsed = time.monotonic() - started

    assert res.status_code == 307, res.text
    assert res.headers.get("location") == "http://localhost/login?sso_error=token"
    assert elapsed < 1.0, elapsed

    rows = await _callback_failure_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].detail["reason"] == "timeout"
    assert rows[0].detail["last_phase"] == "token_ok"


@pytest.mark.asyncio
async def test_two_individually_fast_calls_still_trip_the_shared_deadline(
    session_factory, google_config, monkeypatch
) -> None:
    """L3. The bound is *aggregate*, not per call.

    Budget 0.6s; the POST sleeps 0.4s and the GET sleeps 0.4s. Neither
    call alone exceeds the budget, so a ``timeout_at`` opened freshly
    around each call lets both complete and the handler returns the
    success redirect with no ``sso_error`` at all. Only one absolute
    deadline shared by both blocks catches this.

    The 0.4 / 0.6 ratio is load-bearing and must not be "tidied". It
    leaves 50% headroom per call: shrink the budget toward 0.4 and the
    per-call implementation *also* times out, at which point this test
    passes against the wrong code and stops fencing anything.

    An active user matching the fake's userinfo email is seeded so the
    un-timed-out path genuinely succeeds — the assertion is structural
    (is there an ``sso_error``?), not a wall-clock measurement.
    """
    await _seed_default_plan(session_factory)
    await _seed_user(session_factory, email="alice@acme.io")
    monkeypatch.setattr(auth_module, "GOOGLE_OAUTH_TOTAL_TIMEOUT_S", 0.6)
    _patch_httpx(monkeypatch, hang_on="both", delay_s=0.4)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "matching-state")
        res = client.get(
            "/api/v1/auth/google/callback",
            params={"code": "dummy", "state": "matching-state"},
            follow_redirects=False,
        )

    assert res.status_code == 307, res.text
    location = res.headers.get("location", "")
    assert "sso_error=" in location, location

    rows = await _callback_failure_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].detail["reason"] == "timeout"


EXCHANGE_TIMEOUT_EVENT = "auth.google.callback.exchange_timeout"


def _exchange_timeout_warnings(logger_mock) -> list[dict]:
    """The kwargs of every ``_LOGGER.warning(EXCHANGE_TIMEOUT_EVENT, ...)``
    call, in order. Same seam and same shape as ``_phase_calls`` in
    test_auth_google_callback_breadcrumbs.py."""
    return [
        call.kwargs
        for call in logger_mock.warning.call_args_list
        if call.args and call.args[0] == EXCHANGE_TIMEOUT_EVENT
    ]


@pytest.mark.asyncio
async def test_exchange_timeout_emits_the_ungated_warning_at_the_login_site(
    session_factory, google_config, monkeypatch
) -> None:
    """Fence for the ungated timeout warning at ``/google/callback``.

    *Kills:* deleting the ``_LOGGER.warning(...)`` call from the
    ``except TimeoutError`` clause.

    This warning is not decoration. The biggest production risk in this
    change is the aggregate bound landing below the real p99 of a
    healthy exchange, silently converting working sign-ins into
    ``?sso_error=token`` — a failure that is fast, terminal and total,
    on a public endpoint, and that surfaces as an unexplained
    conversion drop with no 5xx. This warning is the second of the
    three defences against that: it is what makes the failure loud
    before it is large. An emitter that nothing red-gates can be
    deleted by a future refactor with the entire suite still green,
    which is this repo's most-repeated defect. Load-bearing operational
    machinery gets a fence like any other code.

    ``auth_debug_logging`` is pinned False to prove the warning is
    *ungated*: the neighbouring ``_log_google_callback_phase``
    breadcrumbs are gated on that flag and are therefore silent in
    production, which is why a wedged exchange produces no signal today.

    ``timeout_s`` is asserted against the constant read back from
    ``auth_module`` rather than a literal, so the test pins "the
    emitter reports the budget it actually used" without coupling to
    the monkeypatched harness value.
    """
    await _seed_default_plan(session_factory)
    await _seed_user(session_factory, email="alice@acme.io")
    monkeypatch.setattr(app_settings, "auth_debug_logging", False)
    monkeypatch.setattr(auth_module, "GOOGLE_OAUTH_TOTAL_TIMEOUT_S", 0.05)
    _patch_httpx(monkeypatch, hang_on="post", delay_s=2.0)

    app = _make_app(session_factory)
    with patch.object(auth_module, "_LOGGER") as logger_mock:
        with TestClient(app) as client:
            client.cookies.set("oauth_state", "matching-state")
            res = client.get(
                "/api/v1/auth/google/callback",
                params={"code": "dummy", "state": "matching-state"},
                follow_redirects=False,
            )

    assert res.status_code == 307, res.text

    calls = _exchange_timeout_warnings(logger_mock)
    assert len(calls) == 1, calls
    extra = calls[0]["extra"]
    assert extra["flow"] == "login"
    assert extra["last_phase"] == "start"
    assert extra["timeout_s"] == auth_module.GOOGLE_OAUTH_TOTAL_TIMEOUT_S


@pytest.mark.asyncio
async def test_exchange_timeout_warning_fires_on_the_timeout_path_only(
    session_factory, google_config, monkeypatch
) -> None:
    """Negative control for the warning above.

    *Kills:* an emitter hoisted out of the ``except TimeoutError``
    clause — into the ``except httpx.HTTPError`` branch, into a shared
    ``finally``, or onto the main line. Any of those would fill
    production logs with false timeout warnings during an ordinary
    Google outage, destroying the "previously-empty bucket starts
    filling" signal that makes this warning worth having at all.

    Two paths that must stay silent: a genuine httpx transport error,
    and an entirely successful sign-in.
    """
    await _seed_default_plan(session_factory)
    await _seed_user(session_factory, email="alice@acme.io")

    # (1) genuine httpx error — lands in the httpx clause, not the
    # timeout clause.
    _patch_httpx(monkeypatch, raise_on_request=True)
    app = _make_app(session_factory)
    with patch.object(auth_module, "_LOGGER") as logger_mock:
        with TestClient(app) as client:
            client.cookies.set("oauth_state", "matching-state")
            res = client.get(
                "/api/v1/auth/google/callback",
                params={"code": "dummy", "state": "matching-state"},
                follow_redirects=False,
            )
    assert res.status_code == 307, res.text
    assert _exchange_timeout_warnings(logger_mock) == []

    # (2) fully successful callback.
    _patch_httpx(monkeypatch)
    app = _make_app(session_factory)
    with patch.object(auth_module, "_LOGGER") as logger_mock:
        with TestClient(app) as client:
            client.cookies.set("oauth_state", "matching-state")
            res = client.get(
                "/api/v1/auth/google/callback",
                params={"code": "dummy", "state": "matching-state"},
                follow_redirects=False,
            )
    assert res.status_code == 302, res.text
    assert _exchange_timeout_warnings(logger_mock) == []


class _ProgrammerBug(Exception):
    """A bug in our own code, not a transport failure."""


@pytest.mark.asyncio
async def test_non_timeout_exception_is_not_swallowed_by_the_timeout_clause(
    session_factory, google_config, monkeypatch
) -> None:
    """L5. Fences the *upper* bound of the new ``except`` clause.

    ``except TimeoutError`` must stay exactly that. Widening it to
    ``except Exception`` would turn every genuine bug in the exchange —
    starting with the live ``tokens['access_token']`` KeyError — into a
    friendly "try again" banner plus an audit row blaming Google, and
    the rest of this file would stay green while it happened.

    A programmer error must propagate and must leave no audit row.
    """
    _patch_httpx(monkeypatch, raise_exc=_ProgrammerBug("not a timeout"))

    app = _make_app(session_factory)
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "matching-state")
        with pytest.raises(_ProgrammerBug):
            client.get(
                "/api/v1/auth/google/callback",
                params={"code": "dummy", "state": "matching-state"},
                follow_redirects=False,
            )

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


# ── cancelled / provider_error / missing_code ───────────────────────────────


@pytest.mark.asyncio
async def test_google_callback_user_cancelled_redirects_with_cancelled_code(
    session_factory, google_config
) -> None:
    """User clicked Cancel/Back on Google's consent screen. Google
    redirects with ``?error=access_denied&state=<csrf>`` and no
    ``code``. Pre-fix the missing ``code`` required-query 422'd before
    our handler ran, leaving the user on App Platform's generic error
    page. Now we route to /login?sso_error=cancelled with audit row."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        # No cookie set is fine — we want a friendly message even if
        # the state cookie also got nuked.
        res = client.get(
            "/api/v1/auth/google/callback",
            params={
                "error": "access_denied",
                "state": "some-state-value",
                "error_description": "The user cancelled the request",
            },
            follow_redirects=False,
        )

    assert res.status_code == 307, res.text
    assert (
        res.headers.get("location") == "http://localhost/login?sso_error=cancelled"
    )

    rows = await _callback_failure_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].detail["reason"] == "cancelled"
    assert rows[0].detail["google_error"] == "access_denied"
    assert rows[0].detail["google_error_description"] == "The user cancelled the request"


@pytest.mark.asyncio
async def test_google_callback_provider_error_redirects_with_provider_error_code(
    session_factory, google_config
) -> None:
    """Google returned a non-access_denied error (e.g. server_error,
    invalid_request). Map to ``provider_error`` so the banner copy
    distinguishes the cancelled case from a provider issue."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.get(
            "/api/v1/auth/google/callback",
            params={"error": "server_error", "state": "some-state-value"},
            follow_redirects=False,
        )

    assert res.status_code == 307, res.text
    assert (
        res.headers.get("location")
        == "http://localhost/login?sso_error=provider_error"
    )

    rows = await _callback_failure_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].detail["reason"] == "provider_error"
    assert rows[0].detail["google_error"] == "server_error"


@pytest.mark.asyncio
async def test_google_callback_missing_code_and_error_redirects_with_token_code(
    session_factory, google_config
) -> None:
    """Truly malformed callback: no ``code``, no ``error``. Surface
    the existing ``token`` banner copy to the user (no new UI), but
    audit ``reason: "missing_code"`` so ops can tell it apart from
    a real token-exchange failure."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.get(
            "/api/v1/auth/google/callback",
            params={"state": "some-state-value"},
            follow_redirects=False,
        )

    assert res.status_code == 307, res.text
    assert res.headers.get("location") == "http://localhost/login?sso_error=token"

    rows = await _callback_failure_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].detail == {"reason": "missing_code"}


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
