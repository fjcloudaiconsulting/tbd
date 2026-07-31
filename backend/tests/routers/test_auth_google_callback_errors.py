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
import json
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
    """Canned httpx-style response.

    ``payload`` is typed ``Any``, not ``dict``: the payload-validation
    fences drive bodies that are lists, strings and numbers, which is
    exactly what the handler used to assume could never arrive.

    It is also resolved with an explicit ``is None`` check rather than
    truthiness. ``{}`` and ``[]`` are falsy and are two of the bodies
    those fences stub, so a ``payload or {...}`` default would silently
    rewrite them into a usable body and the fence would pass against
    unmodified code — vacuously.

    ``json_exc``, when supplied, is raised from ``.json()`` instead of a
    body being returned: the "200 carrying an HTML error page" case,
    where httpx raises ``json.JSONDecodeError``.
    """

    def __init__(
        self,
        status_code: int,
        payload: Any = None,
        *,
        json_exc: BaseException | None = None,
    ):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self._json_exc = json_exc

    def json(self) -> Any:
        if self._json_exc is not None:
            raise self._json_exc
        return self._payload


def _patch_httpx(
    monkeypatch,
    *,
    token_status: int = 200,
    token_payload: Any = None,
    token_json_exc: BaseException | None = None,
    userinfo_status: int = 200,
    userinfo_payload: Any = None,
    userinfo_json_exc: BaseException | None = None,
    raise_on_request: bool = False,
    raise_exc: BaseException | None = None,
    raise_exc_on: str = "post",
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

    ``raise_exc`` raises an arbitrary exception from the call named by
    ``raise_exc_on`` (``"post"``, the default, or ``"get"``). That is
    deliberately distinct from ``raise_on_request`` so a test can prove
    the handler's ``except`` clauses do *not* swallow a programmer
    error, at either phase of the exchange.

    ``token_payload`` / ``userinfo_payload`` are resolved with an
    explicit ``is None`` check so a falsy-but-meaningful body (``{}``,
    ``[]``) reaches the handler intact — see ``_FakeResponse``.
    ``token_json_exc`` / ``userinfo_json_exc`` make the matching
    ``.json()`` raise instead of decoding.
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
            if raise_exc is not None and raise_exc_on == "post":
                raise raise_exc
            if raise_on_request:
                import httpx
                raise httpx.HTTPError("boom")
            return _FakeResponse(
                token_status,
                token_payload
                if token_payload is not None
                else {"access_token": "fake-token"},
                json_exc=token_json_exc,
            )

        async def get(self, *args: Any, **kwargs: Any) -> _FakeResponse:
            if hang_on in ("get", "both"):
                await asyncio.sleep(delay_s)
            if raise_exc is not None and raise_exc_on == "get":
                raise raise_exc
            if raise_on_request:
                import httpx
                raise httpx.HTTPError("boom")
            return _FakeResponse(
                userinfo_status,
                userinfo_payload
                if userinfo_payload is not None
                else {
                    "email": "alice@acme.io",
                    "verified_email": True,
                    "given_name": "Alice",
                    "family_name": "A",
                },
                json_exc=userinfo_json_exc,
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

    The fields are asserted at the TOP level of the call kwargs, never
    under an ``extra`` key. ``_LOGGER`` is a structlog stdlib
    BoundLogger, which treats ``extra`` as an ordinary key and renders
    it as a nested object, so a DigitalOcean log filter on
    ``flow:"login"`` would not match one. Pinning the flat shape is what
    keeps this warning filterable, which is the only reason it is worth
    emitting.
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
    fields = calls[0]
    assert "extra" not in fields, fields
    assert fields["flow"] == "login"
    assert fields["last_phase"] == "start"
    assert fields["timeout_s"] == auth_module.GOOGLE_OAUTH_TOTAL_TIMEOUT_S


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


# ── TBD-267: Google 200-body shape validation ───────────────────────────────
#
# Both callback sites trusted a 200 response body without validating
# its shape. ``tokens['access_token']`` raised ``KeyError`` on a token
# body without the key; ``google_user.get(...)`` raised
# ``AttributeError`` on a userinfo body that decoded to a list or a
# scalar — and that read sits *outside* the ``try``, so no ``except``
# clause of any width could ever have reached it. Both produced a bare
# 500: no audit row, and App Platform's error splash instead of the
# friendly banner.
#
# The fix is two pure helpers plus four inline ``audit; return`` guards
# inside the existing ``try``. The set of exceptions each handler's
# ``try`` catches is byte-identical before and after — no third
# ``except`` clause anywhere — so the L5/S5 upper-bound fences stay
# valid without being re-derived.


# Comfortably past CPython's nesting limit, which is a C-stack check
# and therefore not a fixed number. ~40 KB of brackets.
_JSON_NEST_DEPTH = 20_000


def _json_decode_error() -> ValueError:
    """The exact exception httpx raises for a 200 carrying HTML.

    ``Response.json()`` is ``json.loads(self.content)``, so a body that
    is not JSON surfaces as ``json.JSONDecodeError`` — a ``ValueError``
    subclass, which is what the helper's ``except`` catches.

    Called *per test*, never once at import. A parametrize table is
    built at collection time, so an instance placed directly in the
    table is one object shared by every test in it, re-raised as many
    times as there are legs. Exceptions are mutable — ``__traceback__``
    accumulates on each raise — so a shared instance couples tests that
    are meant to be independent.
    """
    return json.JSONDecodeError("Expecting value", "<html>not json</html>", 0)


def _recursion_error() -> RecursionError:
    """A REAL ``RecursionError`` out of ``json.loads``.

    Produced by decoding a deeply nested body, never by constructing
    the class. The claim under test is a claim about what
    ``json.loads`` does with a hostile body; a hand-built instance
    would prove only that the ``except`` tuple lists the name.

    ``RecursionError`` derives from ``RuntimeError``, *not* from
    ``ValueError``. TBD-267 shipped the opposite claim ("the only
    body-dependent failures are ``JSONDecodeError`` and
    ``UnicodeDecodeError``, both ``ValueError``") in three places, so
    ``except ValueError`` let this escape ``_google_json_object`` and
    land as the bare 500 with no audit row that the change exists to
    remove.

    The raise is asserted here rather than assumed from the depth:
    CPython's limit is a C-stack check, so a depth that quietly stopped
    tripping it would turn every fence built on this factory into a
    silent pass.
    """
    body = "[" * _JSON_NEST_DEPTH + "]" * _JSON_NEST_DEPTH
    try:
        json.loads(body)
    except RecursionError as exc:
        return exc
    raise AssertionError(
        f"json.loads did not recurse at depth {_JSON_NEST_DEPTH}: "
        "raise the depth, or every fence using this factory is vacuous"
    )


def test_google_json_object_propagates_a_programmer_error() -> None:
    """U1. Fences the *upper* bound of the helper's own ``except``.

    *Kills:* ``except Exception`` (or a bare ``except``) inside
    ``_google_json_object``.

    This is the one widened-clause hazard the whole change introduces:
    L5/S5 fence the handlers' ``try`` blocks, but neither can reach a
    clause nested inside the helper, because the helper swallows before
    the handler ever sees it. A helper that ate everything would turn
    an arbitrary bug in our own decode path into a friendly "try again"
    banner plus an audit row blaming Google — exactly the failure mode
    L5/S5 exist to prevent, relocated one frame deeper where they
    cannot see it.

    ``(ValueError, RecursionError)`` and nothing wider is the correct
    width. TBD-267's original justification for ``ValueError`` alone —
    "the only body-dependent failures of ``json.loads`` are
    ``JSONDecodeError`` and ``UnicodeDecodeError``, both
    ``ValueError``" — was false: a deeply nested body raises
    ``RecursionError``, a ``RuntimeError`` subclass. See U4, which
    proves it by decoding rather than by assertion. Those three are the
    complete body-dependent set; anything else out of ``.json()`` is
    our bug and must keep propagating.
    """
    resp = _FakeResponse(200, json_exc=_ProgrammerBug("not a decode failure"))
    with pytest.raises(_ProgrammerBug):
        auth_module._google_json_object(resp)


def test_google_json_object_absorbs_a_real_recursion_error() -> None:
    """U4. The *lower* bound of the helper's ``except``, at its second name.

    *Kills:* narrowing the clause back to ``except ValueError``.

    Two claims, both executable, in order. First that ``json.loads``
    really does answer a deeply nested body with ``RecursionError``
    rather than any ``ValueError`` — the premise TBD-267 got wrong.
    Then that the helper absorbs it into the ``None`` sentinel like
    every other unusable body, instead of letting it escape to the
    browser as a bare 500 with no audit row.

    ``.json()`` here performs a genuine decode. Stubbing a
    pre-built exception instance would fence the ``except`` tuple but
    not the claim about ``json.loads`` that the tuple's width rests on,
    and that claim is exactly what was wrong.
    """

    class _RealDecodeResponse:
        """``json()`` that decodes for real, the way httpx's does."""

        def __init__(self, text: str) -> None:
            self._text = text

        def json(self) -> Any:
            return json.loads(self._text)

    body = "[" * _JSON_NEST_DEPTH + "]" * _JSON_NEST_DEPTH
    with pytest.raises(RecursionError) as caught:
        json.loads(body)
    # Not a ValueError. The whole defect in one assertion.
    assert not isinstance(caught.value, ValueError)

    assert auth_module._google_json_object(_RealDecodeResponse(body)) is None


@pytest.mark.parametrize(
    "payload, json_exc_factory, expected",
    [
        (None, _json_decode_error, None),
        (None, _recursion_error, None),
        ([], None, None),
        (["a", "b"], None, None),
        ("a string", None, None),
        (7, None, None),
        (None, None, {}),
        ({"a": 1}, None, {"a": 1}),
    ],
    ids=[
        "decode-error",
        "recursion-error",
        "empty-list",
        "list",
        "string",
        "number",
        "empty-dict",
        "dict",
    ],
)
def test_google_json_object_returns_none_for_every_non_object_body(
    payload: Any, json_exc_factory: Any, expected: Any
) -> None:
    """U2. The helper's contract: a dict, or ``None``. Never anything else.

    *Kills two named wrong implementations.*

    (1) A fix that only checks ``"access_token" in tokens``. That
    handles the ``KeyError`` and leaves the ``AttributeError`` on the
    userinfo side fully alive, because a list has no ``.get`` and
    ``in`` on a list is perfectly legal. The userinfo half of the
    defect is the half no ``except`` clause could ever have caught, so
    a fix that only closes the token half closes the easy one.

    (2) A helper that returns ``{}`` instead of ``None`` for a
    non-object body. That reads as harmless — the guards would still
    fire, since ``{}`` has no ``access_token`` and no ``email`` — but
    it erases the distinction the audit row is for: ``not_object``
    ("Google sent us something that is not a JSON object at all")
    would become indistinguishable from ``no_access_token`` ("Google
    sent a well-formed object that is missing the field"), which are
    different incidents with different remediations.

    ``{}`` is deliberately in the table as a *pass-through*: an empty
    JSON object is still an object, and must come back as itself rather
    than collapsing into the ``None`` sentinel.

    The exception legs are *factories*, called here rather than in the
    table, so each leg raises its own instance — see
    ``_json_decode_error``.
    """
    json_exc = json_exc_factory() if json_exc_factory is not None else None
    resp = _FakeResponse(200, payload, json_exc=json_exc)
    result = auth_module._google_json_object(resp)
    if expected is None:
        # ``is None``, not ``== None``: ``{}``, ``[]`` and ``0`` all
        # compare falsy, and collapsing them would be the exact
        # confusion this helper exists to remove.
        assert result is None
    else:
        assert result == expected
        assert isinstance(result, dict)


def test_google_token_body_detail_never_leaks_a_credential() -> None:
    """U3. The forensic detail dict is a shape word, not a body dump.

    *Kills:* a future "let me just dump the body so I can debug this"
    edit to ``_google_token_body_detail``.

    This dict is persisted to ``audit_events.detail`` and rendered in
    /admin/audit. A token response that fails our usability check can
    still be *partially* valid — Google may have returned
    ``refresh_token`` and ``id_token`` alongside an ``access_token`` we
    rejected — so the body is live credential material. The helper is
    the only place in the change that reads a field off an untrusted
    Google token body, which makes it the only place that edit could
    land.

    The OAuth2 ``error`` code is the one exception and is safe by
    contract: RFC 6749 §5.2 defines it as a fixed enum of failure
    codes. It is truncated anyway, because "Google's field is
    documented as short" is not a bound we control.

    The first fixture carries an *empty* ``access_token``, not a usable
    one. As shipped it read ``"access_token": "s"`` — a perfectly good
    ASCII token — and asserted ``no_access_token``, which described a
    state the helper can never see in production: it is only ever
    called after the guard rejected the token, and the guard accepts
    ``"s"``. The empty string is the real shape that reaches here with
    other credentials still live alongside it.
    """
    detail = auth_module._google_token_body_detail(
        {
            "access_token": "",
            "refresh_token": "r",
            "id_token": "i",
            "error": "invalid_grant",
        }
    )
    assert detail == {"body": "no_access_token", "google_error": "invalid_grant"}
    assert "refresh_token" not in detail
    assert "access_token" not in detail
    assert "id_token" not in detail

    # A token that is *present* but of the wrong JSON type gets its own
    # shape word. Both of these used to report ``no_access_token``,
    # which tells an operator Google returned no token when it returned
    # one — the wrong first move, and unfalsifiable from the audit row.
    assert auth_module._google_token_body_detail(
        {"access_token": {"nested": 1}}
    ) == {"body": "bad_access_token_type"}
    assert auth_module._google_token_body_detail({"access_token": 12345}) == {
        "body": "bad_access_token_type"
    }
    assert auth_module._google_token_body_detail({"access_token": ["a"]}) == {
        "body": "bad_access_token_type"
    }
    # ``null`` is absence, not a type error: JSON has no other way to
    # spell "the field is not set".
    assert auth_module._google_token_body_detail({"access_token": None}) == {
        "body": "no_access_token"
    }
    # And the wrong-type word must not carry the value either.
    leaky = auth_module._google_token_body_detail(
        {"access_token": {"SENTINEL": "CREDENTIAL"}}
    )
    assert "SENTINEL" not in str(leaky)

    long = auth_module._google_token_body_detail({"error": "x" * 5000})
    assert long["google_error"] == "x" * 64
    assert len(long["google_error"]) == 64

    # The ``unusable_access_token`` shape word: a token that is present
    # but that httpx could not ASCII-encode into the Authorization
    # header. It still must not appear in the detail.
    unusable = auth_module._google_token_body_detail(
        {"access_token": "ünusable-SENTINEL"}
    )
    assert unusable == {"body": "unusable_access_token"}
    assert "SENTINEL" not in str(unusable)

    assert auth_module._google_token_body_detail(None) == {"body": "not_object"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token_payload, expected_detail",
    [
        (
            {"error": "invalid_grant", "error_description": "Bad Request"},
            {
                "reason": "token_payload",
                "body": "no_access_token",
                "google_error": "invalid_grant",
            },
        ),
        (
            {"access_token": ""},
            {"reason": "token_payload", "body": "no_access_token"},
        ),
        (
            {"access_token": 12345},
            {"reason": "token_payload", "body": "bad_access_token_type"},
        ),
        (
            {"access_token": {"nested": 1}},
            {"reason": "token_payload", "body": "bad_access_token_type"},
        ),
    ],
    ids=["oauth-error-body", "empty-token", "number-token", "object-token"],
)
async def test_token_200_without_access_token_redirects_and_audits(
    session_factory,
    google_config,
    monkeypatch,
    token_payload: dict[str, Any],
    expected_detail: dict[str, Any],
) -> None:
    """L6. Google answered 200 with an OAuth2 error body, no token.

    Pre-fix this was ``tokens['access_token']`` → ``KeyError`` →
    bare 500: no audit row at all, and App Platform's "Error / check
    logs" splash instead of the /login banner. It is the single most
    likely shape of this defect in production, because a 200 carrying
    ``{"error": "invalid_grant"}`` is what Google returns for a replayed
    or expired authorization code.

    The audit ``detail`` is asserted as an EXACT dict, not by key. Two
    things ride on that. The ``reason``/``body`` split is the
    vocabulary ops greps on, and an exact match is what stops a later
    edit widening the row into a body dump — ``error_description`` is
    free text from Google and is deliberately *not* carried, unlike the
    provider-error branch above, which carries it because there is no
    token body in play there.

    Three legs beyond the original OAuth-error body, each pinning a leg
    of the guard predicate that no test reached:

    - ``""`` — the ``not access_token`` term. An implementation
      dropping it accepts an empty bearer token and sends
      ``Authorization: Bearer `` to Google.
    - ``12345`` / ``{"nested": 1}`` — the ``isinstance`` term, and the
      ``bad_access_token_type`` shape word. Both used to report
      ``no_access_token``: an operator reading /admin/audit was told
      Google returned no token when Google returned one of the wrong
      type, which points the investigation at credentials instead of at
      whatever is rewriting the body.
    """
    monkeypatch.setattr(app_settings, "auth_debug_logging", False)
    _patch_httpx(monkeypatch, token_payload=token_payload)

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
    assert len(rows) == 1, rows
    assert rows[0].outcome.value == "failure"
    assert rows[0].actor_user_id is None
    assert rows[0].actor_email == ""
    assert rows[0].detail == expected_detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs_factory",
    [
        lambda: {"token_payload": []},
        lambda: {"token_json_exc": _json_decode_error()},
        lambda: {"token_json_exc": _recursion_error()},
    ],
    ids=["json-array", "not-json-at-all", "recursion"],
)
async def test_token_200_with_a_non_object_body_redirects_and_audits(
    session_factory, google_config, monkeypatch, kwargs_factory
) -> None:
    """L7. The 200 whose body is not a JSON object at all.

    Three real shapes, one branch: a JSON array (a proxy or WAF
    substituting its own payload), a body that does not decode as JSON
    at all (an HTML interstitial served with a 200, which is what a
    captive portal or a misrouted CDN does), and a body nested deeply
    enough that the decoder itself gives up.

    Pre-fix the first raised ``TypeError`` on the string subscript and
    the second raised ``JSONDecodeError`` out of ``.json()`` — different
    exceptions, same bare 500, same missing audit row.

    The ``recursion`` leg is the one TBD-267 shipped *without* closing.
    ``except ValueError`` does not catch ``RecursionError``, so the
    exchange died exactly as it did before the change: no row, platform
    splash. About 40 KB of ``[`` is the whole attack. See
    ``_recursion_error``.

    ``detail`` is asserted EXACT and must carry no ``google_error``
    key: there is no object to read an ``error`` field off, and
    inventing one would make an unparseable body look like a
    provider-reported OAuth failure in /admin/audit.
    """
    monkeypatch.setattr(app_settings, "auth_debug_logging", False)
    _patch_httpx(monkeypatch, **kwargs_factory())

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
    assert len(rows) == 1, rows
    assert rows[0].detail == {"reason": "token_payload", "body": "not_object"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs_factory",
    [
        lambda: {"userinfo_payload": ["not", "a", "dict"]},
        lambda: {"userinfo_json_exc": _json_decode_error()},
        lambda: {"userinfo_json_exc": _recursion_error()},
    ],
    ids=["json-array", "not-json-at-all", "recursion"],
)
async def test_userinfo_200_with_a_non_object_body_redirects_and_audits(
    session_factory, google_config, monkeypatch, kwargs_factory
) -> None:
    """L8. The half of the defect no ``except`` clause could reach.

    ``google_user.get("email", "")`` sits *after* the ``try/except``
    block, on the main line. A userinfo 200 whose body decodes to a
    list raises ``AttributeError`` there, outside every handler the
    function has. Widening ``except TimeoutError`` to ``except
    Exception`` would not catch it; neither would adding a third
    clause. This is the concrete reason the fix is ``isinstance``
    validation and not exception handling, and this test is what makes
    that argument executable rather than rhetorical.

    ``sso_error=userinfo`` and ``reason="userinfo_payload"``: the user
    sees the existing userinfo banner copy (already mapped in all three
    frontend copy dicts, so no frontend change), while ops can tell a
    non-200 userinfo response apart from a 200 with a broken body.

    ``actor_email`` stays ``""``: the whole point of this branch is
    that we never got a readable email, so there is nothing to attach.

    Two legs beyond the JSON array pin the *decode* half of this
    branch, which had no coverage at all: ``userinfo_json_exc`` was
    plumbed through the harness and passed by no test, so an
    implementation that called ``userinfo_resp.json()`` raw and only
    ``isinstance``-checked the result passed the whole suite while
    still 500ing on an HTML interstitial. The ``recursion`` leg is the
    ``RecursionError`` TBD-267's ``except ValueError`` misses.

    The warning is asserted here, not just the redirect and the row.
    L10 covers the token phase exclusively, so the userinfo
    ``_LOGGER.warning`` at this site was deletable with the suite
    green — which contradicts the change's own rationale that the
    warning is the only production signal left once a 5xx becomes a
    quiet 307. The field set is exact for the same reason it is exact
    in L10: structured-log fields are the emitter's contract.
    """
    monkeypatch.setattr(app_settings, "auth_debug_logging", False)
    _patch_httpx(monkeypatch, **kwargs_factory())

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
    assert res.headers.get("location") == "http://localhost/login?sso_error=userinfo"

    calls = _invalid_payload_warnings(logger_mock)
    assert len(calls) == 1, calls
    fields = calls[0]
    assert "extra" not in fields, fields
    assert set(fields) == {"flow", "phase", "body"}, fields
    assert fields["flow"] == "login"
    assert fields["phase"] == "userinfo"
    assert fields["body"] == "not_object"

    rows = await _callback_failure_rows(session_factory)
    assert len(rows) == 1, rows
    assert rows[0].actor_email == ""
    assert rows[0].detail == {"reason": "userinfo_payload", "body": "not_object"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_email",
    [None, ["alice@acme.io"], 12345, {"address": "alice@acme.io"}, True],
    ids=["null", "list", "number", "object", "bool"],
)
async def test_non_string_email_lands_on_the_no_email_branch(
    session_factory, google_config, monkeypatch, bad_email: Any
) -> None:
    """L12. The shape guard validates the container, not the fields.

    *Kills:* reading the email back as ``google_user.get("email", "")``.

    ``_google_json_object`` proves the userinfo body is a dict. It says
    nothing about what is *in* the dict, and ``.get(key, default)``
    substitutes the default only for a **missing key** — never for an
    explicit ``null``, a list or a number. Each of those reached
    ``normalize_email(...).strip()`` on the main line, outside every
    ``try``, and raised ``AttributeError``: bare 500, no audit row, the
    platform splash. Which is the same failure TBD-267 was written to
    remove, one level down, on a body that passes its new guard.

    ``null`` is the shape that matters most. It is what an OIDC
    provider emits for a claim it has but cannot populate, and it is
    also the one this site handles *worse* than the step-up site, which
    already wrote ``(google_user.get("email") or "")``.

    The landing spot is deliberately the **existing** ``no_email``
    branch rather than a new audit reason. An operator's move is
    identical either way — the user's Google account gave us no usable
    address — and a second vocabulary word for it would be one more
    string to grep with no different remediation behind it.

    No user row may be created: an account keyed on a coerced empty
    email would be an authentication defect, not a cosmetic one.
    """
    monkeypatch.setattr(app_settings, "auth_debug_logging", False)
    _patch_httpx(
        monkeypatch,
        userinfo_payload={
            "email": bad_email,
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
    assert res.headers.get("location") == "http://localhost/login?sso_error=no_email"

    rows = await _callback_failure_rows(session_factory)
    assert len(rows) == 1, rows
    assert rows[0].detail == {"reason": "no_email"}
    assert rows[0].actor_email == ""

    async with session_factory() as db:
        assert (await db.scalars(select(User))).all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True], ids=["new-user", "existing-user"])
async def test_non_string_profile_fields_do_not_break_the_callback(
    session_factory, google_config, monkeypatch, existing: bool
) -> None:
    """L13. L12's siblings: the *optional* userinfo fields.

    *Kills:* reading ``given_name`` / ``family_name`` / ``picture``
    straight off the validated dict.

    Three more uncaught 500s lived past the new shape guard, all after
    the ``try``, all reachable with a body that is a perfectly good
    JSON object:

    - ``given_name: 99`` → ``TypeError: sequence item 0: expected str
      instance, int found``, inside ``_suggest_username``'s
      ``" ".join(parts)``.
    - ``picture: 12345`` → ``TypeError: object of type 'int' has no
      len()`` inside ``_safe_avatar_url``.
    - ``picture: {"url": "x"}`` is the worst of the three. A dict has a
      ``len()`` of 1, so it passes ``_safe_avatar_url`` *unchanged*, is
      assigned to ``user.avatar_url``, and dies at ``commit`` with
      ``ProgrammingError: type 'dict' is not supported`` — after ORM
      state has already been mutated.

    None of these is a *failure*: a display name or an avatar that
    arrives wrong-typed is a missing optional field. So the assertion
    is that the callback **succeeds** with the field dropped, and emits
    no audit row. Adding a reason here would turn a cosmetic gap into a
    blocked sign-in.

    Both branches are driven because they read the fields at different
    places: the existing-user branch backfills onto a live ORM object
    (the ``commit`` crash above), the new-user branch feeds
    ``_suggest_username`` and the ``User(...)`` constructor. A fix
    applied to one and not the other passes half of this test.
    """
    await _seed_default_plan(session_factory)
    if existing:
        await _seed_user(session_factory, email="alice@acme.io", username="alice")
    _patch_httpx(
        monkeypatch,
        userinfo_payload={
            "email": "alice@acme.io",
            "verified_email": True,
            "given_name": 99,
            "family_name": 100,
            "picture": {"url": "https://example.test/a.png"},
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

    assert res.status_code == 302, res.text
    assert res.headers.get("location", "").startswith(
        "http://localhost/auth/google/callback#token="
    ), res.headers.get("location")

    assert await _callback_failure_rows(session_factory) == []

    async with session_factory() as db:
        user = await db.scalar(select(User).where(User.email == "alice@acme.io"))
        assert user is not None
        # Dropped, never coerced to the repr of a number or a dict.
        assert not user.first_name, user.first_name
        assert not user.last_name, user.last_name
        assert user.avatar_url is None, user.avatar_url
        if not existing:
            # ``_suggest_username`` found no usable name parts and fell
            # through to the email local part, which is its documented
            # behaviour for a nameless Google account.
            assert user.username == "alice"


@pytest.mark.asyncio
async def test_programmer_error_at_the_userinfo_call_is_not_swallowed(
    session_factory, google_config, monkeypatch
) -> None:
    """L9. L5's twin, at the *second* bounded block.

    *Kills:* an ``except Exception`` added around the userinfo half of
    the exchange. L5 drives its programmer error from the token POST,
    so a widened clause reachable only after the token phase — or a
    ``try`` re-drawn to wrap just the userinfo call — would leave L5
    green. The two payload guards this change adds both sit after their
    respective network calls, which makes "just wrap it in a try" the
    obvious wrong turn at exactly this point in the function.
    """
    _patch_httpx(
        monkeypatch,
        raise_exc=_ProgrammerBug("not a timeout"),
        raise_exc_on="get",
    )

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


INVALID_PAYLOAD_EVENT = "auth.google.callback.invalid_payload"


def _invalid_payload_warnings(logger_mock) -> list[dict]:
    """The kwargs of every ``_LOGGER.warning(INVALID_PAYLOAD_EVENT, ...)``
    call, in order. Same seam as ``_exchange_timeout_warnings``."""
    return [
        call.kwargs
        for call in logger_mock.warning.call_args_list
        if call.args and call.args[0] == INVALID_PAYLOAD_EVENT
    ]


@pytest.mark.asyncio
async def test_invalid_payload_emits_the_ungated_warning_at_the_login_site(
    session_factory, google_config, monkeypatch
) -> None:
    """L10. Fence for the ungated warning on the new guard.

    *Kills:* deleting the ``_LOGGER.warning(...)`` call from the token
    payload guard.

    This warning is a net-visibility requirement, not decoration.
    Today this failure class is loud in the worst way: it screams as a
    5xx stack trace in the platform logs. After the fix it is a quiet
    307 that looks exactly like an ordinary user-side failure, so
    shipping the guard *without* the warning would trade a bare 500 for
    a silent one — a real loss of production signal on a public
    endpoint, and precisely the kind of unfenced operational machinery
    a later refactor deletes with the suite still green.

    ``auth_debug_logging`` is pinned False to prove the warning is
    ungated: the neighbouring ``_log_google_callback_phase``
    breadcrumbs are gated on that flag and are silent in production.

    Fields are asserted at the TOP level of the call kwargs, never
    under ``extra``. ``_LOGGER`` is a structlog stdlib BoundLogger,
    which treats ``extra`` as an ordinary key and renders it nested, so
    a DigitalOcean log filter on ``flow:"login"`` would not match one.

    The second leg is a credential check with teeth. It drives the
    ``unusable_access_token`` branch — a token Google really did send,
    which we reject because httpx would fail to ASCII-encode it into the
    Authorization header *inside* the bounded block — and asserts the
    token value never reaches the log line. That is the only stub in
    which a real credential is in play, so it is the only one that can
    fence the leak. It is also the branch's sole end-to-end exercise.

    The key set is asserted exactly, in both legs. Structured-log
    fields are the emitter's contract, and an exact set is what stops a
    later "add a bit more context here" edit widening the line toward
    the body.
    """
    monkeypatch.setattr(app_settings, "auth_debug_logging", False)
    _patch_httpx(
        monkeypatch,
        token_payload={
            "error": "invalid_grant",
            "error_description": "Bad Request",
        },
    )

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

    calls = _invalid_payload_warnings(logger_mock)
    assert len(calls) == 1, calls
    fields = calls[0]
    assert "extra" not in fields, fields
    assert set(fields) == {"flow", "phase", "body", "google_error"}, fields
    assert fields["flow"] == "login"
    assert fields["phase"] == "token"
    assert fields["body"] == "no_access_token"
    assert fields["google_error"] == "invalid_grant"

    # Second leg: a real credential is present and must not be logged.
    secret = "ünusable-SENTINEL-CREDENTIAL"
    _patch_httpx(monkeypatch, token_payload={"access_token": secret})
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
    calls = _invalid_payload_warnings(logger_mock)
    assert len(calls) == 1, calls
    fields = calls[0]
    assert set(fields) == {"flow", "phase", "body"}, fields
    assert fields["body"] == "unusable_access_token"
    assert "SENTINEL" not in str(fields), fields

    rows = await _callback_failure_rows(session_factory)
    assert len(rows) == 2, rows
    assert rows[-1].detail == {
        "reason": "token_payload",
        "body": "unusable_access_token",
    }


@pytest.mark.asyncio
async def test_invalid_payload_warning_fires_on_the_payload_paths_only(
    session_factory, google_config, monkeypatch
) -> None:
    """L11. Negative control for L10.

    *Kills:* the emitter hoisted off the guard — onto the main line,
    into a ``finally``, or into the ``except`` clauses. Any of those
    would fill production logs with false ``invalid_payload`` warnings
    on healthy sign-ins and during ordinary Google outages, destroying
    the "previously-empty bucket starts filling" signal that is the
    only reason the warning is worth emitting.

    Four paths that must stay silent, chosen to cover each way the
    handler can leave the exchange: success, a non-200 token exchange,
    a transport error, and the aggregate timeout. The timeout leg also
    re-asserts ``reason="timeout"`` — the guards are inserted between
    the network calls and their audit writes, so a guard placed one
    line off could shadow the timeout branch's own audit row (TBD-179's
    forensic signal) while every timeout test that only checks the
    status code stayed green.
    """
    await _seed_default_plan(session_factory)
    await _seed_user(session_factory, email="alice@acme.io")
    monkeypatch.setattr(app_settings, "auth_debug_logging", False)

    # (1) fully successful callback.
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
    assert _invalid_payload_warnings(logger_mock) == []

    # (2) non-200 token exchange — the status_code branch, which sits
    # strictly before the payload guard and must stay untouched.
    _patch_httpx(monkeypatch, token_status=400)
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
    assert _invalid_payload_warnings(logger_mock) == []

    # (3) genuine httpx transport error.
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
    assert _invalid_payload_warnings(logger_mock) == []

    # (4) the aggregate timeout — still audits reason="timeout".
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
    assert _invalid_payload_warnings(logger_mock) == []

    timeout_rows = [
        row
        for row in await _callback_failure_rows(session_factory)
        if row.detail and row.detail.get("reason") == "timeout"
    ]
    assert len(timeout_rows) == 1, timeout_rows


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
