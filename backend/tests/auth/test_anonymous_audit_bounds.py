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

Rate-limiter notes. The singleton bleeds between tests, so ``reset_limiter``
runs before AND after. Under ``TestClient`` the peer is the literal
``"testclient"`` (it fails ``_is_trusted_proxy``, so ``get_client_ip`` returns
it unchanged), giving one deterministic key per module. Storage is
``MemoryStorage`` in CI (``REDIS_URL`` unset) and Redis in the dev container;
both count identically and ``reset()`` works on both.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

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

    Kills the over-tight mutant ``if actor_user_id is not None:`` alone.
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
    session_factory, google_config, path, event_type, qkey, base
):
    """?code=... with a MISMATCHED cookie -> 307 ``state``, and ZERO rows.

    ⚠⚠ THE HALF-FIX DOOR. Conditioning only the ``error`` and ``missing_code``
    branches does not close the hole, it MOVES it: a caller who omits or
    mismatches the cookie falls through to the state branch and gets an
    identical unbounded anonymous row. ``reason="state"`` is reachable ONLY
    when the state check fails, so that row is anonymous by construction and
    nothing legitimate is lost by suppressing it.
    """
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
