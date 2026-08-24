"""TBD-353 — bound the anonymous audit-write surface on the public auth routes.

Three public routes wrote an ``audit_events`` row on a call carrying no
credential of any kind, and six unlimited public auth routes carried no
``@limiter.limit`` decorator at all. An anonymous caller could therefore
inflate the table ``/admin/audit`` reads and bury real security events.

Spec: ``specs/2026-08-22-tbd-353-anonymous-audit-write-bounds.md``.

What each group fences, and the wrong implementation it kills:

* **F1 — the five limits.** Calls 1..N assert the route's *exact* normal
  status; call N+1 asserts 429. The exact status on both legs is what kills
  the loosened-number mutant (``60/minute`` -> ``600/minute``), which a
  "a 429 happens eventually" assertion survives. Leg 1 is also the ticket's
  required control: a legitimate call inside the budget still succeeds with
  its normal side effect.

  ⚠ Downstream-guard trap. ``_validate_google_config()`` raises 501 when the
  client id is unset, and slowapi increments *before* the handler runs — so a
  429 would still fire while every earlier call was a 501 and the fence proved
  nothing about a legitimate call. Every Google leg uses ``google_config`` and
  asserts the pre-limit status is 200/307. A 501 cannot produce a 307.

* **F2 — the ``/logout`` vacuous-row gate.** The predicate is
  ``if sids or actor_user_id is not None`` — two guards, so four legs.
  ⚠ ``jtis_seen`` is deliberately NOT a third term and no leg here claims to
  exercise one: ``decode_refresh_jti_sid`` raises unless both claims are
  present, so ``jtis_seen`` non-empty implies ``sids`` non-empty and a third
  term would be dead. Leg 2 proves the decode-failure path writes no row; it
  exercises no distinct term and is not labelled as if it did.

* **F3 — callback state conditioning.** ⚠⚠ The rule is that NO
  ``_record_google_callback_failure`` may run on any path where the state
  check fails, the ``state`` branch included. Conditioning only the ``error``
  and ``missing_code`` branches does not close the hole, it MOVES it: a caller
  who omits the cookie falls through to the state branch and gets an identical
  unbounded anonymous row. Leg 7 is that door.

* **F4 — a CHARACTERIZATION test, green against ``main`` by design.** It pins
  behaviour we deliberately keep, so a later ``verify_exp: False`` cannot land
  as a silent no-op. It is not a regression fence and must not be read as one.
  ⚠ It carries a POSITIVE CONTROL: ``assert calls == []`` is a no-op-shaped
  assertion that a detached spy satisfies for free, so the same test drives a
  LIVE cookie through and asserts the spy fires. Without it, rewriting
  ``auth.py`` to ``from app.redis_client import session_revoke_family`` would
  make this test vacuously green AND silently disarm its documented mutant.

* **F6 — the step-up callback's four POST-``state_ok`` suppressions.** The
  shape check, the ``int(parts[1])`` parse, the unknown ``return_key`` and the
  missing ``User`` all sit BELOW the state check, so they run with
  ``state_ok == True`` and are NOT covered by F3's "anonymous by construction"
  argument. Nothing in the repo drove a ``stepup:``-shaped state at a fence
  asserting audit rows, so flipping any of the four back to ``audit=True``
  left the entire suite green. Each leg supplies a MATCHING ``oauth_state``
  cookie plus a ``stepup:``-shaped state that trips exactly one branch, and
  asserts zero rows, the byte-exact ``Location``, and the event NAME.
  ⚠ The event name is asserted because the two suppressed populations are not
  interchangeable: ``unverified_state`` is forged drive-by noise an operator
  filters out in bulk, while ``invalid_state_payload`` is the user-id sweep
  and must stay alertable. Emitting both under one name is the mislabelling
  this group fences.

Structlog assertions bind a recorder onto ``auth._LOGGER`` and never use
``structlog.testing.capture_logs()``: that swaps the processor chain on the
GLOBAL structlog config, which other modules in this suite reconfigure without
restoring, so a ``capture_logs`` fence is green alone, green on either half,
and RED in a full run.

Rate-limiter notes. The singleton bleeds between tests, so ``reset_limiter``
runs before AND after. Under ``TestClient`` the peer is the literal
``"testclient"`` (it fails ``_is_trusted_proxy``, so ``get_client_ip`` returns
it unchanged), giving one deterministic key per module. Storage is
``MemoryStorage`` in CI (``REDIS_URL`` unset) and Redis in the dev container;
both count identically and ``reset()`` works on both.

⚠ The limiter is a PROCESS SINGLETON, so ``sso-stepup/callback``'s ``60/hour``
is effectively per test SESSION, not per test. This module burns 61 of the 60
in F1 alone plus ~5 more across F3/F6 — the autouse reset clears them, but that
leaves roughly 39 calls of suite-wide headroom before a cross-module 429 flake
becomes possible. Any new module driving that route must carry the same reset.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import httpx
import jwt
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
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers import auth as auth_module
from app.routers.auth import router as auth_router
from app.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
)

from tests.conftest import set_refresh_cookie

PASSWORD = "starting-password-1"

GOOGLE_CB = "/api/v1/auth/google/callback"
STEPUP_CB = "/api/v1/auth/sso-stepup/callback"

# The five limits this PR ships, as (route, method, limit-count). Kept as
# literals so a retune of the decorator without a matching test edit fails.
LOGOUT_LIMIT = 120
RESET_PASSWORD_LIMIT = 10
GOOGLE_LIMIT = 60
GOOGLE_CALLBACK_LIMIT = 60
STEPUP_CALLBACK_LIMIT = 60


@pytest.fixture
def fake_redis(_autouse_fake_redis):
    yield _autouse_fake_redis


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
    """Fill the Google OAuth knobs so ``_validate_google_config`` passes.

    Load-bearing, not cosmetic: without it every Google leg 501s before the
    handler body, and the 429 assertion would pass while proving nothing.
    """
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


async def _seed_user(factory, *, username: str = "alice") -> dict:
    async with factory() as db:
        org = Organization(name=f"Acme-{username}", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id,
            username=username,
            email=f"{username}@example.com",
            password_hash=hash_password(PASSWORD),
            role=Role.OWNER,
            is_active=True,
            email_verified=True,
        )
        db.add(user)
        await db.commit()
        return {"org_id": org.id, "user_id": user.id, "email": user.email}


async def _rows(factory, event_type: str) -> list[AuditEvent]:
    async with factory() as db:
        res = await db.execute(
            select(AuditEvent).where(AuditEvent.event_type == event_type)
        )
        return list(res.scalars().all())


async def _terminated(factory) -> list[AuditEvent]:
    return await _rows(factory, "auth.session.terminated")


class _LogRecorder:
    """Collects events emitted on ``app.routers.auth._LOGGER``.

    ⚠ Deliberately NOT ``structlog.testing.capture_logs()``. That swaps the
    processor chain on the GLOBAL structlog config, and modules in this suite
    call ``structlog.configure(...)`` without restoring it, so a capture_logs
    fence is green on its own file, green on either half of the suite, and RED
    in a full run. Binding onto the module's own logger is immune to all of it.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def _record(self, event: str, **kw) -> None:
        self.events.append((event, kw))

    debug = info = warning = error = exception = _record

    def callback_events(self) -> list[tuple[str, dict]]:
        """Only the two OAuth-callback suppression events."""
        return [
            (name, kw)
            for name, kw in self.events
            if name.startswith("auth.oauth.callback.")
        ]


def _delete_cookies(headers) -> list[str]:
    out = []
    for key, value in headers.raw:
        if key.decode().lower() == "set-cookie":
            v = value.decode()
            if v.split("=", 1)[0].strip() == "refresh_token" and "Max-Age=0" in v:
                out.append(v)
    return out


# ══════════════════════════════════════════════════════════════════════════
# F1 — the five rate limits. Exact boundary on every route.
# ══════════════════════════════════════════════════════════════════════════


async def test_f1_logout_rate_limited_at_exact_boundary(session_factory, fake_redis):
    """Calls 1..120 return 200; call 121 returns 429.

    Kills: the decorator deleted, and the number loosened.
    Leg 1 doubles as the DoD control — a legitimate anonymous logout inside
    the budget still returns 200 AND still emits both delete-cookie headers.
    """
    app = _make_app(session_factory)
    with TestClient(app) as client:
        first = client.post("/api/v1/auth/logout")
        assert first.status_code == 200, first.text
        assert first.json()["detail"] == "Logged out"
        assert len(_delete_cookies(first.headers)) >= 2, first.headers.raw

        for i in range(2, LOGOUT_LIMIT + 1):
            res = client.post("/api/v1/auth/logout")
            assert res.status_code == 200, f"call {i}: {res.status_code} {res.text}"

        over = client.post("/api/v1/auth/logout")
    assert over.status_code == 429, over.text


async def test_f1_reset_password_rate_limited_at_exact_boundary(session_factory):
    """Calls 1..10 return 400 (invalid token); call 11 returns 429."""
    app = _make_app(session_factory)
    body = {"token": "not-a-real-token", "new_password": "Whatever-123456"}
    with TestClient(app) as client:
        for i in range(1, RESET_PASSWORD_LIMIT + 1):
            res = client.post("/api/v1/auth/reset-password", json=body)
            assert res.status_code == 400, f"call {i}: {res.status_code} {res.text}"
        over = client.post("/api/v1/auth/reset-password", json=body)
    assert over.status_code == 429, over.text


async def test_f1_google_login_rate_limited_at_exact_boundary(
    session_factory, google_config
):
    """Calls 1..60 return 200 with a real redirect_url; call 61 returns 429.

    The 200 assertion is the 501 tripwire: without ``google_config`` every
    earlier call would be a 501 and the 429 would prove nothing.
    """
    app = _make_app(session_factory)
    with TestClient(app) as client:
        for i in range(1, GOOGLE_LIMIT + 1):
            res = client.get("/api/v1/auth/google")
            assert res.status_code == 200, f"call {i}: {res.status_code} {res.text}"
            assert "accounts.google.com" in res.json()["redirect_url"]
        over = client.get("/api/v1/auth/google")
    assert over.status_code == 429, over.text


async def test_f1_google_callback_rate_limited_at_exact_boundary(
    session_factory, google_config
):
    """Calls 1..60 return 307; call 61 returns 429. 307 is the 501 tripwire."""
    app = _make_app(session_factory)
    params = {"error": "access_denied", "state": "s"}
    with TestClient(app) as client:
        for i in range(1, GOOGLE_CALLBACK_LIMIT + 1):
            res = client.get(GOOGLE_CB, params=params, follow_redirects=False)
            assert res.status_code == 307, f"call {i}: {res.status_code} {res.text}"
        over = client.get(GOOGLE_CB, params=params, follow_redirects=False)
    assert over.status_code == 429, over.text


async def test_f1_stepup_callback_rate_limited_at_exact_boundary(
    session_factory, google_config
):
    """Calls 1..60 return 307; call 61 returns 429. 307 is the 501 tripwire."""
    app = _make_app(session_factory)
    params = {"error": "access_denied", "state": "s"}
    with TestClient(app) as client:
        for i in range(1, STEPUP_CALLBACK_LIMIT + 1):
            res = client.get(STEPUP_CB, params=params, follow_redirects=False)
            assert res.status_code == 307, f"call {i}: {res.status_code} {res.text}"
        over = client.get(STEPUP_CB, params=params, follow_redirects=False)
    assert over.status_code == 429, over.text


# ══════════════════════════════════════════════════════════════════════════
# F2 — the /logout vacuous-row gate. Predicate: sids or actor_user_id.
# ══════════════════════════════════════════════════════════════════════════


async def test_f2_leg1_anonymous_logout_writes_no_row_but_still_clears(
    session_factory, fake_redis
):
    """No cookie, no bearer -> 200, both delete-cookie headers, ZERO rows.

    The 200 and the headers are asserted deliberately: a mutant that 401s the
    anonymous logout ALSO produces zero rows and would pass a rows-only test.
    """
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post("/api/v1/auth/logout")
    assert res.status_code == 200, res.text
    assert res.json()["detail"] == "Logged out"
    assert len(_delete_cookies(res.headers)) >= 2, res.headers.raw
    assert await _terminated(session_factory) == []


async def test_f2_leg2_corrupt_cookie_logout_writes_no_row(session_factory, fake_redis):
    """Garbage cookie, no bearer -> 200, ZERO rows.

    Proves the decode-failure path writes nothing. It exercises no term the
    other legs do not; it is NOT a ``jtis_seen`` leg (there is no such term).
    """
    app = _make_app(session_factory)
    with TestClient(app) as client:
        set_refresh_cookie(client, "this-is-not-a-jwt")
        res = client.post("/api/v1/auth/logout")
    assert res.status_code == 200, res.text
    assert len(_delete_cookies(res.headers)) >= 2
    assert await _terminated(session_factory) == []


async def test_f2_leg3_bearer_only_logout_writes_row(session_factory, fake_redis):
    """Valid bearer, no cookie -> ONE row with sid_count == 0.

    Kills the over-tight mutant ``if sids:``, which would silently drop the
    row for an authenticated user whose refresh cookie had already expired.
    """
    seeded = await _seed_user(session_factory)
    token = create_access_token(seeded["user_id"], seeded["org_id"], Role.OWNER.value)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"}
        )
    assert res.status_code == 200, res.text
    rows = await _terminated(session_factory)
    assert len(rows) == 1, rows
    assert rows[0].detail["sid_count"] == 0
    assert rows[0].actor_user_id == seeded["user_id"]


async def test_f2_leg4_valid_cookie_no_bearer_writes_row(session_factory, fake_redis):
    """Signature-valid cookie, empty Redis, no bearer -> ONE row, sid_count 1.

    ⚠ This leg does NOT kill ``if actor_user_id is not None:``, and an earlier
    docstring here claimed it did. Measured: the row comes back with
    ``actor_user_id == 1``, because ``logout`` falls back to the refresh JWT's
    own ``sub`` when no bearer is present — so that mutant, ``if sids:``, and
    deleting the guard outright ALL survive this leg untouched.
    **Leg 5 is the leg that kills it**, using a ``sub``-less refresh JWT, and
    its docstring explains why. Do not delete leg 5 as redundant with this one.

    What this leg genuinely pins is the cookie-only path's detail payload:
    a decodable cookie yields ``sid_count == 1`` while an empty Redis family
    yields ``jti_count == 0``. That pair is what a "count the jtis, not the
    sids" mutant breaks.
    """
    seeded = await _seed_user(session_factory)
    token, _jti, _sid = create_refresh_token(seeded["user_id"])
    app = _make_app(session_factory)
    with TestClient(app) as client:
        set_refresh_cookie(client, token)
        res = client.post("/api/v1/auth/logout")
    assert res.status_code == 200, res.text
    rows = await _terminated(session_factory)
    assert len(rows) == 1, rows
    assert rows[0].detail["sid_count"] == 1
    assert rows[0].detail["jti_count"] == 0
    # ⚠ The measurement behind the warning above, pinned rather than left in
    # prose: the cookie's own ``sub`` becomes the actor, so BOTH terms of the
    # predicate are true on this leg and none of the three mutants can be
    # distinguished here.
    assert rows[0].actor_user_id == seeded["user_id"], rows[0].actor_user_id


async def test_f2_leg5_sid_without_resolvable_actor_writes_row(
    session_factory, fake_redis
):
    """A revoked family with NO resolvable actor still writes its row.

    ⚠ This leg exists because the obvious one does NOT discriminate. Leg 4
    presents an ordinary refresh cookie, but ``logout`` falls back to the
    refresh JWT's own ``sub`` when the bearer is missing, so an ordinary
    cookie sets ``actor_user_id`` too — and the over-tight mutant
    ``if actor_user_id is not None:`` survives leg 4 untouched. Measured:
    it did.

    The isolating fixture is a signature-valid refresh JWT carrying ``jti``
    and ``sid`` but no ``sub``: ``decode_refresh_jti_sid`` accepts it (it
    requires only ``type``, ``jti``, ``sid``), while ``decode_token``
    returns a payload whose ``sub`` is ``None``, so the actor fallback
    finds nothing. A family is therefore revoked with no actor to name —
    and that revoke MUST still be recorded, or a real state change goes
    unaudited.

    Kills: ``if actor_user_id is not None:`` (the ``sids`` term dropped).
    """
    seeded = await _seed_user(session_factory, username="subless")
    now = datetime.now(timezone.utc)
    subless = jwt.encode(
        {
            "type": "refresh",
            "jti": "no-sub-jti",
            "sid": "no-sub-sid",
            "iat": now,
            "exp": now + timedelta(days=1),
        },
        app_settings.jwt_secret_key,
        algorithm=app_settings.jwt_algorithm,
    )
    assert seeded["user_id"]  # the org/user exist; the token just names nobody

    app = _make_app(session_factory)
    with TestClient(app) as client:
        set_refresh_cookie(client, subless)
        res = client.post("/api/v1/auth/logout")

    assert res.status_code == 200, res.text
    rows = await _terminated(session_factory)
    assert len(rows) == 1, rows
    assert rows[0].detail["sid_count"] == 1
    assert rows[0].actor_user_id is None


# ══════════════════════════════════════════════════════════════════════════
# F3 — callback state conditioning. Parametrised over BOTH callbacks.
# ══════════════════════════════════════════════════════════════════════════

# (path, event_type, error-query-key, base redirect origin)
CALLBACKS = [
    pytest.param(
        GOOGLE_CB,
        "auth.google.callback.failed",
        "sso_error",
        "http://localhost/login",
        id="google",
    ),
    pytest.param(
        STEPUP_CB,
        "auth.google.sso_stepup.callback.failed",
        "sso_stepup_error",
        "http://localhost/settings",
        id="stepup",
    ),
]


@pytest.mark.parametrize("path,event_type,qkey,base", CALLBACKS)
async def test_f3_leg1_error_without_cookie_writes_no_row(
    session_factory, google_config, path, event_type, qkey, base
):
    """?error=access_denied, NO cookie -> identical 307, ZERO rows."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.get(
            path,
            params={"error": "access_denied", "state": "some-state-value"},
            follow_redirects=False,
        )
    assert res.status_code == 307, res.text
    assert res.headers["location"] == f"{base}?{qkey}=cancelled"
    assert await _rows(session_factory, event_type) == []


@pytest.mark.parametrize("path,event_type,qkey,base", CALLBACKS)
async def test_f3_leg2_error_with_matching_cookie_retains_row(
    session_factory, google_config, path, event_type, qkey, base
):
    """?error=access_denied WITH a matching cookie -> identical 307, ONE row.

    Kills the "just delete the audit call" pseudo-fix: a real user who cancels
    at the consent screen keeps their forensic row, with detail intact.
    """
    app = _make_app(session_factory)
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "some-state-value")
        res = client.get(
            path,
            params={
                "error": "access_denied",
                "state": "some-state-value",
                "error_description": "The user cancelled the request",
            },
            follow_redirects=False,
        )
    assert res.status_code == 307, res.text
    assert res.headers["location"] == f"{base}?{qkey}=cancelled"
    rows = await _rows(session_factory, event_type)
    assert len(rows) == 1, rows
    assert rows[0].detail["reason"] == "cancelled"
    assert rows[0].detail["google_error"] == "access_denied"
    assert (
        rows[0].detail["google_error_description"] == "The user cancelled the request"
    )


@pytest.mark.parametrize("path,event_type,qkey,base", CALLBACKS)
async def test_f3_leg3_provider_error_with_matching_cookie_retains_row(
    session_factory, google_config, path, event_type, qkey, base
):
    """?error=server_error WITH a matching cookie -> ONE row, provider_error."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "some-state-value")
        res = client.get(
            path,
            params={"error": "server_error", "state": "some-state-value"},
            follow_redirects=False,
        )
    assert res.status_code == 307, res.text
    assert res.headers["location"] == f"{base}?{qkey}=provider_error"
    rows = await _rows(session_factory, event_type)
    assert len(rows) == 1, rows
    assert rows[0].detail["reason"] == "provider_error"


@pytest.mark.parametrize("path,event_type,qkey,base", CALLBACKS)
async def test_f3_leg4_missing_code_without_cookie_writes_no_row(
    session_factory, google_config, path, event_type, qkey, base
):
    """No code, no error, NO cookie -> identical 307 (?...=token), ZERO rows."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.get(
            path, params={"state": "some-state-value"}, follow_redirects=False
        )
    assert res.status_code == 307, res.text
    assert res.headers["location"] == f"{base}?{qkey}=token"
    assert await _rows(session_factory, event_type) == []


@pytest.mark.parametrize("path,event_type,qkey,base", CALLBACKS)
async def test_f3_leg5_missing_code_with_matching_cookie_retains_row(
    session_factory, google_config, path, event_type, qkey, base
):
    """No code, no error, WITH a matching cookie -> ONE row, missing_code."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "some-state-value")
        res = client.get(
            path, params={"state": "some-state-value"}, follow_redirects=False
        )
    assert res.status_code == 307, res.text
    assert res.headers["location"] == f"{base}?{qkey}=token"
    rows = await _rows(session_factory, event_type)
    assert len(rows) == 1, rows
    assert rows[0].detail["reason"] == "missing_code"


@pytest.mark.parametrize("path,event_type,qkey,base", CALLBACKS)
async def test_f3_leg6_error_with_mismatched_cookie_keeps_cancelled_code(
    session_factory, google_config, path, event_type, qkey, base
):
    """?error=access_denied with a MISMATCHED cookie -> ZERO rows, and the
    redirect is still ``cancelled``, NOT ``state``.

    This is the leg that discriminates CONDITIONING from REORDERING. An
    implementer who literally moves the state check above the error branch
    produces ``=state`` here and goes red, while passing every row-count leg.
    """
    app = _make_app(session_factory)
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "a-different-value")
        res = client.get(
            path,
            params={"error": "access_denied", "state": "some-state-value"},
            follow_redirects=False,
        )
    assert res.status_code == 307, res.text
    assert res.headers["location"] == f"{base}?{qkey}=cancelled"
    assert await _rows(session_factory, event_type) == []


@pytest.mark.parametrize("path,event_type,qkey,base", CALLBACKS)
async def test_f3_leg7_state_branch_writes_no_row(
    session_factory, google_config, monkeypatch, path, event_type, qkey, base
):
    """?code=... with a MISMATCHED cookie -> 307 ``state``, and ZERO rows.

    ⚠⚠ THE HALF-FIX DOOR. Conditioning only the ``error`` and ``missing_code``
    branches does not close the hole, it MOVES it: a caller who omits or
    mismatches the cookie falls through to the state branch and gets an
    identical unbounded anonymous row. ``reason="state"`` is reachable ONLY
    when the state check fails, so that row is anonymous by construction and
    nothing legitimate is lost by suppressing it.

    Also pins the event NAME. A mismatched state IS forged-drive-by-shaped, so
    this path must log ``unverified_state``; the four step-up branches in F6
    must NOT. Collapsing the two names back into one reddens exactly one of
    the two groups, whichever name survives.
    """
    recorder = _LogRecorder()
    monkeypatch.setattr(auth_module, "_LOGGER", recorder)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "a-different-value")
        res = client.get(
            path,
            params={"code": "some-code", "state": "some-state-value"},
            follow_redirects=False,
        )
    assert res.status_code == 307, res.text
    assert res.headers["location"] == f"{base}?{qkey}=state"
    assert await _rows(session_factory, event_type) == []

    logged = recorder.callback_events()
    assert len(logged) == 1, logged
    name, payload = logged[0]
    assert name == "auth.oauth.callback.unverified_state", logged
    assert payload["state_ok"] is False, payload
    assert payload["reason"] == "state", payload


@pytest.mark.parametrize("path,event_type,qkey,base", CALLBACKS)
async def test_f3_leg8_non_ascii_state_does_not_500(
    session_factory, google_config, path, event_type, qkey, base
):
    """A non-ASCII ``state`` redirects, it does not 500.

    ``secrets.compare_digest`` raises ``TypeError`` on ``str`` operands
    containing non-ASCII, and ``state`` is an attacker-controlled query
    parameter — so the naive ``compare_digest(cookie, state)`` turns a state
    mismatch into a 500. The comparison must be on ``.encode()`` bytes.
    """
    app = _make_app(session_factory)
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "ascii-cookie-value")
        res = client.get(
            path,
            params={"code": "some-code", "state": "éé-non-ascii"},
            follow_redirects=False,
        )
    assert res.status_code == 307, res.text
    assert res.headers["location"] == f"{base}?{qkey}=state"
    assert await _rows(session_factory, event_type) == []


@pytest.mark.parametrize("path,event_type,qkey,base", CALLBACKS)
async def test_f3_leg9_retained_row_truncates_attacker_text(
    session_factory, google_config, path, event_type, qkey, base
):
    """A multi-KB ``error_description`` is truncated in the RETAINED row.

    ⚠ This is the OTHER half of bounding the anonymous write surface, and
    suppressing rows does not cover it. With a matching ``oauth_state`` cookie
    the caller reaches the retained writer at the route's full budget, and
    ``google_error``/``google_error_description`` are copied straight out of
    the query string into a JSON column. Capping the row COUNT while leaving
    the row SIZE unbounded just changes the units of the same inflation.

    The sibling public writer already settled the shape:
    ``routers/security.py`` bounds every persisted field at
    ``_MAX_FIELD_LEN = 512``. ``auth._MAX_AUDIT_DETAIL_FIELD_LEN`` matches it.

    Kills: the truncation dropped from ``_record_google_callback_failure``,
    and a cap applied to only one of the two fields.
    """
    long_description = "A" * 4096
    long_error = "B" * 4096
    app = _make_app(session_factory)
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "some-state-value")
        res = client.get(
            path,
            params={
                "error": long_error,
                "state": "some-state-value",
                "error_description": long_description,
            },
            follow_redirects=False,
        )
    assert res.status_code == 307, res.text
    # A non-``access_denied`` error is a provider_error; the redirect is
    # unchanged by the truncation, which only touches the stored detail.
    assert res.headers["location"] == f"{base}?{qkey}=provider_error"

    rows = await _rows(session_factory, event_type)
    assert len(rows) == 1, rows
    detail = rows[0].detail
    assert detail["google_error_description"] == "A" * 512, len(
        detail["google_error_description"]
    )
    assert detail["google_error"] == "B" * 512, len(detail["google_error"])
    # The un-truncated fields survive intact — truncation must not become
    # a blanket mangling of every detail value.
    assert detail["reason"] == "provider_error"


# ══════════════════════════════════════════════════════════════════════════
# F4 — CHARACTERIZATION (green against main by design), see module docstring.
# ══════════════════════════════════════════════════════════════════════════


async def test_f4_expired_refresh_cookie_logout_does_not_revoke_family(
    session_factory, fake_redis, monkeypatch
):
    """An EXPIRED but signature-valid refresh cookie does not revoke a family.

    ⚠ This test is GREEN against unmodified ``main``. It is a characterization
    test, not a regression fence: it pins the behaviour TBD-353 deliberately
    KEEPS, so that a future "fix" implementing the old comment's claim with
    ``options={"verify_exp": False}`` cannot land as a silent no-op.

    Why we keep it: the JWT ``exp``, the cookie ``Max-Age``, the Redis primary
    TTL and the family-set TTL are all the same ``ttl_seconds`` set together,
    so a jti whose JWT has expired has no live primary key to revoke, and any
    family still alive is named by an unexpired head cookie that decodes fine.

    Mutant that turns it red: give ``decode_refresh_jti_sid`` the
    ``verify_exp: False`` option.

    ⚠ ``assert calls == []`` is a no-op-shaped assertion: a spy that was never
    wired satisfies it for free. The second half of this test is the POSITIVE
    CONTROL — the same spy, a LIVE cookie, and an assertion that it fires. If
    ``auth.py`` ever switches to ``from app.redis_client import
    session_revoke_family``, the monkeypatch stops intercepting and the
    control goes red, instead of this test going vacuously green and its
    documented mutant going quietly undetectable.
    """
    seeded = await _seed_user(session_factory)
    now = datetime.now(timezone.utc)
    expired = jwt.encode(
        {
            "sub": str(seeded["user_id"]),
            "type": "refresh",
            "jti": "expired-jti",
            "sid": "expired-sid",
            "iat": now - timedelta(days=2),
            "exp": now - timedelta(days=1),
        },
        app_settings.jwt_secret_key,
        algorithm=app_settings.jwt_algorithm,
    )

    calls: list[str] = []
    from app import redis_client as rc

    async def _spy(sid: str):
        calls.append(sid)
        return []

    monkeypatch.setattr(rc, "session_revoke_family", _spy)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        set_refresh_cookie(client, expired)
        res = client.post("/api/v1/auth/logout")

    assert res.status_code == 200, res.text
    assert len(_delete_cookies(res.headers)) >= 2
    assert calls == [], f"expired cookie must not revoke a family, got {calls}"

    # ── positive control: the same spy MUST fire for a live cookie ──────
    live_token, _live_jti, live_sid = create_refresh_token(seeded["user_id"])
    with TestClient(app) as client:
        set_refresh_cookie(client, live_token)
        live_res = client.post("/api/v1/auth/logout")

    assert live_res.status_code == 200, live_res.text
    assert calls == [live_sid], (
        "the spy is not wired: a live refresh cookie must reach "
        f"session_revoke_family, got {calls}"
    )


# ══════════════════════════════════════════════════════════════════════════
# F6 — the step-up callback's four POST-``state_ok`` suppressions.
# ══════════════════════════════════════════════════════════════════════════
#
# ⚠⚠ These four sit BELOW ``if not state_ok:``, so they run with
# ``state_ok == True``: the state DID round-trip and the payload inside it
# was junk. F3's "anonymous by construction" argument therefore does NOT
# cover them, and neither did any fence — before this group, flipping any of
# the four back to ``audit=True`` left the ENTIRE suite green, because
# nothing in the repo drove a ``stepup:``-shaped state at a test that
# counts audit rows. This is the repo's "a fence must record the PATH, not
# just the item" rule applied to the PR's own fences.
#
# (state, expected return path, id). Each state trips exactly ONE branch:
# the earlier branches must pass for the later one to be reachable, so the
# tuples are ordered the way the handler is.
STEPUP_PAYLOAD_BRANCHES = [
    # len(parts) != 4 — the legacy 3-part shape. ``_resolve_return_path``
    # cannot read slot 4, so it falls back to the default target.
    pytest.param("stepup:1:nonce", "/settings", id="bad_shape"),
    # int(parts[1]) raises. Slot 4 IS readable here, so the redirect lands
    # on /settings/security — which is what makes the Location assertion
    # discriminating rather than four copies of the same string.
    pytest.param(
        "stepup:notanint:nonce:security", "/settings/security", id="user_id_not_int"
    ),
    # return_key not in _STEPUP_RETURN_TARGETS — falls back to the default.
    pytest.param("stepup:1:nonce:bogus_key", "/settings", id="unknown_return_key"),
    # user is None. A user IS seeded (id 1), so 999999 exercises the lookup
    # miss rather than an empty table.
    pytest.param("stepup:999999:nonce:settings", "/settings", id="missing_user"),
]


@pytest.mark.parametrize("state,return_path", STEPUP_PAYLOAD_BRANCHES)
async def test_f6_stepup_payload_branch_writes_no_row(
    session_factory, google_config, monkeypatch, state, return_path
):
    """Matching cookie + a ``stepup:``-shaped junk state -> ZERO rows.

    Kills: ``audit=False`` flipped back to ``audit=True`` on this branch.

    Also pins the event NAME. The state round-tripped, so this is NOT
    forged-drive-by traffic and must not be logged as
    ``unverified_state`` — an operator who correctly bulk-filters that
    name would otherwise lose the ``missing_user`` sweep entirely, which
    is a REGRESSION against the durable audit row it used to leave.
    """
    await _seed_user(session_factory)
    recorder = _LogRecorder()
    monkeypatch.setattr(auth_module, "_LOGGER", recorder)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        client.cookies.set("oauth_state", state)
        res = client.get(
            STEPUP_CB,
            params={"code": "some-code", "state": state},
            follow_redirects=False,
        )

    assert res.status_code == 307, res.text
    assert (
        res.headers["location"]
        == f"http://localhost{return_path}?sso_stepup_error=state"
    ), res.headers["location"]
    assert await _rows(session_factory, "auth.google.sso_stepup.callback.failed") == []

    logged = recorder.callback_events()
    assert len(logged) == 1, logged
    name, payload = logged[0]
    assert name == "auth.oauth.callback.invalid_state_payload", logged
    assert payload["state_ok"] is True, payload
    assert payload["reason"] == "state", payload


async def test_f6_control_matching_state_reaches_the_exchange(
    session_factory, google_config, monkeypatch
):
    """CONTROL: a WELL-FORMED stepup state past all four branches behaves
    differently — it reaches the token exchange.

    Without this, every F6 leg above is satisfied by a handler that 307s
    ``?sso_stepup_error=state`` unconditionally, and the four legs would prove
    only that the route redirects. Here the same cookie/state round trip, with
    a real user id and a known return key, gets past the payload branches and
    fails LATER (``sso_stepup_error=token``, from the outbound exchange), with
    ``invalid_state_payload`` NOT logged.
    """
    seeded = await _seed_user(session_factory)
    state = f"stepup:{seeded['user_id']}:nonce:settings"
    recorder = _LogRecorder()
    monkeypatch.setattr(auth_module, "_LOGGER", recorder)

    async def _boom(*args, **kwargs):
        raise httpx.ConnectError("no network in tests")

    monkeypatch.setattr(httpx.AsyncClient, "post", _boom)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        client.cookies.set("oauth_state", state)
        res = client.get(
            STEPUP_CB,
            params={"code": "some-code", "state": state},
            follow_redirects=False,
        )

    assert res.status_code == 307, res.text
    # ``httpx.ConnectError`` is an ``httpx.HTTPError``, so the handler takes
    # its exchange-failure branch: a DIFFERENT redirect code, reached only
    # after all four payload branches were passed.
    assert (
        res.headers["location"] == "http://localhost/settings?sso_stepup_error=token"
    ), res.headers["location"]
    assert [name for name, _ in recorder.callback_events()] == [], (
        "a well-formed state must not log a suppressed-callback event"
    )
