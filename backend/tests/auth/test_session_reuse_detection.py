"""Refresh-token REUSE detection — fail-safe family revoke past grace.

Pins the security contract added on top of the PR 3 rotation/grace model:

When a refresh token that was rotated PAST the 30s grace/leeway window is
presented to ``/refresh``, treat it as reuse of an exfiltrated cookie and
revoke the WHOLE session family (fail-safe). Audit only, NO email / NO
in-app notification.

Load-bearing separations verified here:
  * ``/verify`` NEVER revokes (read-only) — a both-miss on /verify is a
    plain 401, family intact.
  * The OTHER 401 reasons that share the "Session has been
    invalidated" detail (iat-cutoff / missing-claim / binding-mismatch)
    NEVER reach the reuse detection.
  * Within the grace window a stale jti is a benign catch-up, not reuse.
  * Detection + revoke is exactly-once (idempotent) => exactly one audit.
  * Session store unreachable => 503, never a revoke-on-uncertainty.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app import state_db
from app.config import settings
from app.database import get_db
from app.deps import get_session_factory
from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers.auth import (
    LEGACY_REFRESH_COOKIE_PATH,
    SESSION_EXPIRED_DETAIL,
    RefreshBothMissError,
    router as auth_router,
)
from app.security import (
    create_refresh_token,
    decode_refresh_jti_sid,
    hash_password,
)

from tests.conftest import expire_grace, set_refresh_cookie, state_family, state_jtis


PASSWORD = "starting-password-1"


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


@pytest.fixture(autouse=True)
def _spy_no_notifications(monkeypatch):
    """Assert the reuse path sends NO email / NO in-app notification.

    Patches the two notification entry points auth.py uses for its OTHER
    (single-user security) events with call counters. The reuse path must
    never touch either; every test can read these counters.
    """
    from app.services import notification_service

    calls = {"dispatch": 0, "security_email": 0}

    async def _dispatch(*args, **kwargs):
        calls["dispatch"] += 1

    async def _security_email(*args, **kwargs):
        calls["security_email"] += 1

    monkeypatch.setattr(
        notification_service, "dispatch_notification_best_effort", _dispatch
    )
    monkeypatch.setattr(
        notification_service, "send_security_email_best_effort", _security_email
    )
    return calls


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


async def _seed_user(factory: async_sessionmaker[AsyncSession]) -> dict:
    async with factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id,
            username="alice",
            email="alice@example.com",
            password_hash=hash_password(PASSWORD),
            role=Role.OWNER,
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


def _canonical_refresh_cookie(headers) -> str | None:
    cookies = _set_cookie_values_for(headers, "refresh_token")
    canonical = [
        c
        for c in cookies
        if "Path=/" in c
        and f"Path={LEGACY_REFRESH_COOKIE_PATH}" not in c
        and "Max-Age=0" not in c
    ]
    return canonical[0] if canonical else None


def _refresh_token_from_set_cookie(raw: str) -> str:
    head = raw.split(";", 1)[0].strip()
    name, _, value = head.partition("=")
    assert name == "refresh_token"
    return value


def _login(client: TestClient) -> str:
    res = client.post(
        "/api/v1/auth/login",
        json={"login": "alice", "password": PASSWORD},
    )
    assert res.status_code == 200, res.text
    raw = _canonical_refresh_cookie(res.headers)
    assert raw is not None
    return _refresh_token_from_set_cookie(raw)


async def _list_audit(
    factory: async_sessionmaker[AsyncSession], event_type: str
) -> list[AuditEvent]:
    async with factory() as db:
        rows = await db.execute(
            select(AuditEvent).where(AuditEvent.event_type == event_type)
        )
        return list(rows.scalars().all())


@asynccontextmanager
async def _httpx_app_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ── 1. Reuse past grace revokes the entire family + audits, no email ─────────


async def test_reuse_past_grace_revokes_family(
    session_factory, _spy_no_notifications
):
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        old_jti, sid = decode_refresh_jti_sid(token)

        # Rotate: old_jti -> winner. Family now {old_jti, winner_jti}.
        set_refresh_cookie(client, token)
        r1 = client.post("/api/v1/auth/refresh")
        assert r1.status_code == 200
        winner_raw = _canonical_refresh_cookie(r1.headers)
        winner_token = _refresh_token_from_set_cookie(winner_raw)
        winner_jti, _ = decode_refresh_jti_sid(winner_token)

        # Simulate rotation PAST the leeway: age the grace window out.
        expire_grace(sid)
        # Sanity: family still holds both jtis, winner is the live head.
        assert old_jti in state_jtis(sid)
        assert state_db._validate(winner_jti) is not None

        # Replay the exfiltrated old cookie past grace => REUSE.
        set_refresh_cookie(client, token)
        res = client.post("/api/v1/auth/refresh")

    assert res.status_code == 401, res.text
    assert res.json()["detail"] == "Session has been invalidated"

    # Whole family gone: family row + every member.
    assert state_family(sid) is None
    assert state_jtis(sid) == set()
    assert state_db._validate(winner_jti) is None
    assert state_db._validate(old_jti) is None

    reuse = await _list_audit(session_factory, "auth.session.reuse_detected")
    assert len(reuse) == 1, f"expected 1 reuse_detected event, got {len(reuse)}"
    assert reuse[0].outcome == "failure"
    assert reuse[0].detail["sid"] == sid
    assert reuse[0].detail["old_jti"] == old_jti
    assert reuse[0].detail["jti_count"] == 2

    # NO email / NO in-app notification.
    assert _spy_no_notifications["dispatch"] == 0
    assert _spy_no_notifications["security_email"] == 0


# ── 2. Within leeway = benign catch-up, no revoke, no reuse audit ────────────


async def test_within_leeway_is_benign(session_factory):
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        old_jti, sid = decode_refresh_jti_sid(token)

        set_refresh_cookie(client, token)
        r1 = client.post("/api/v1/auth/refresh")
        assert r1.status_code == 200
        # old_jti is graced (within leeway).
        assert state_db._grace(old_jti) is not None

        # Replay old cookie WHILE grace is alive => grace catch-up (200).
        set_refresh_cookie(client, token)
        res = client.post("/api/v1/auth/refresh")

    assert res.status_code == 200, res.text
    # Family intact.
    assert state_family(sid) is not None
    reuse = await _list_audit(session_factory, "auth.session.reuse_detected")
    assert reuse == []
    grace = await _list_audit(session_factory, "auth.session.grace_accept")
    assert len(grace) == 1


# ── 3. Garbage jti (not a family member) => plain 401, no revoke ─────────────


async def test_garbage_jti_no_revoke(session_factory):
    from app.security import create_refresh_token

    seeded = await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        _old_jti, sid = decode_refresh_jti_sid(token)

        # Mint a JWT that decodes fine and passes user/iat/claim checks,
        # carrying the LIVE session's sid but a jti that was never issued
        # (not the head, not graced, not a family member).
        bogus_token, bogus_jti, _sid = create_refresh_token(
            seeded["user_id"], sid=sid, jti="never-issued-jti"
        )
        assert bogus_jti not in state_jtis(sid)

        set_refresh_cookie(client, bogus_token)
        res = client.post("/api/v1/auth/refresh")

    assert res.status_code == 401, res.text
    assert res.json()["detail"] == "Session has been invalidated"
    # Family untouched: the real login jti chain is intact.
    assert state_family(sid) is not None
    reuse = await _list_audit(session_factory, "auth.session.reuse_detected")
    assert reuse == []


# ── 4. /verify NEVER revokes on a both-miss token (critical) ─────────────────


async def test_verify_never_revokes_on_both_miss(session_factory):
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        old_jti, sid = decode_refresh_jti_sid(token)

        set_refresh_cookie(client, token)
        r1 = client.post("/api/v1/auth/refresh")
        assert r1.status_code == 200
        winner_raw = _canonical_refresh_cookie(r1.headers)
        winner_jti, _ = decode_refresh_jti_sid(
            _refresh_token_from_set_cookie(winner_raw)
        )

        # Past leeway: age the grace out. old_jti is now a both-miss.
        expire_grace(sid)

        # /verify with the both-miss cookie.
        set_refresh_cookie(client, token)
        res = client.post("/api/v1/auth/verify")

    assert res.status_code == 401, res.text
    assert res.json()["detail"] == "Session has been invalidated"
    # /verify never emits Set-Cookie.
    assert _canonical_refresh_cookie(res.headers) is None
    # CRITICAL: family fully intact, winner still the live head, NO reuse audit.
    assert state_family(sid) is not None
    assert old_jti in state_jtis(sid)
    assert state_db._validate(winner_jti) is not None
    reuse = await _list_audit(session_factory, "auth.session.reuse_detected")
    assert reuse == []


# ── 5. Non-both-miss 401 (binding mismatch) never reaches the reuse Lua ──────


async def test_non_both_miss_401_does_not_revoke(
    session_factory, monkeypatch
):
    # Spy: reuse detection must NOT be invoked for a non-both-miss 401.
    calls = {"n": 0}
    real = state_db.session_detect_reuse_and_revoke

    async def _spy(jti, sid):
        calls["n"] += 1
        return await real(jti, sid)

    monkeypatch.setattr(state_db, "session_detect_reuse_and_revoke", _spy)

    seeded = await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        jti, sid = decode_refresh_jti_sid(token)

        # A JWT carrying the live head jti but another sid: the stored
        # family's sid mismatches the JWT's => row_binding_mismatch (a
        # generic 401, NOT a both-miss: the jti is the head, so the grace
        # fallback never runs).
        token, _, _ = create_refresh_token(
            seeded["user_id"], sid="wrong-sid-not-the-jwt-sid", jti=jti
        )

        set_refresh_cookie(client, token)
        res = client.post("/api/v1/auth/refresh")

    assert res.status_code == 401, res.text
    assert res.json()["detail"] == "Session has been invalidated"
    # Reuse detection was NEVER called.
    assert calls["n"] == 0
    # Family intact, no reuse audit.
    assert state_family(sid) is not None
    reuse = await _list_audit(session_factory, "auth.session.reuse_detected")
    assert reuse == []


# ── 6. Idempotent: two sequential replays revoke once, one audit ─────────────


async def test_reuse_is_idempotent_single_audit(session_factory):
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        old_jti, sid = decode_refresh_jti_sid(token)

        set_refresh_cookie(client, token)
        assert client.post("/api/v1/auth/refresh").status_code == 200
        expire_grace(sid)

        # First replay => reuse + revoke.
        set_refresh_cookie(client, token)
        first = client.post("/api/v1/auth/refresh")
        assert first.status_code == 401
        assert state_family(sid) is None

        # Second replay of the SAME stale jti => family already gone,
        # no such member => "unknown" => plain 401, no second revoke/audit.
        set_refresh_cookie(client, token)
        second = client.post("/api/v1/auth/refresh")
        assert second.status_code == 401

    reuse = await _list_audit(session_factory, "auth.session.reuse_detected")
    assert len(reuse) == 1, f"expected exactly 1 reuse_detected, got {len(reuse)}"


# ── 7. Store error during detection => 503, no revoke-on-uncertainty ─────────


async def test_reuse_store_error_returns_503(
    session_factory, monkeypatch
):
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        old_jti, sid = decode_refresh_jti_sid(token)

        set_refresh_cookie(client, token)
        assert client.post("/api/v1/auth/refresh").status_code == 200
        expire_grace(sid)

        async def _boom(jti, sid):
            raise OperationalError("SELECT", {}, Exception("mysql down"))

        monkeypatch.setattr(
            state_db, "session_detect_reuse_and_revoke", _boom
        )

        set_refresh_cookie(client, token)
        res = client.post("/api/v1/auth/refresh")

    assert res.status_code == 503, res.text
    # Family NOT revoked on uncertainty.
    assert state_family(sid) is not None
    reuse = await _list_audit(session_factory, "auth.session.reuse_detected")
    assert reuse == []


# ── 8. Direct wrapper classification (live / grace / unknown / reused) ───────


async def test_wrapper_classifications():
    sid = "sid-classify"
    state_db._issue("jlive", sid, 1, 3600)

    # live: the family head.
    assert await state_db.session_detect_reuse_and_revoke("jlive", sid) == (
        "live",
    )

    # grace: rotated out inside the window.
    state_db._rotate("jlive", "jgrace-succ", sid, 1, 3600)
    assert await state_db.session_detect_reuse_and_revoke("jlive", sid) == (
        "grace",
    )

    # unknown: not a member.
    assert await state_db.session_detect_reuse_and_revoke("jnope", sid) == (
        "unknown",
    )

    # reused: consumed family member past the grace window.
    expire_grace(sid)
    result = await state_db.session_detect_reuse_and_revoke("jlive", sid)
    assert result == ("reused", 2)
    assert state_family(sid) is None
    assert state_jtis(sid) == set()


# ── 9. RefreshBothMissError carries the snapshot needed for audit ────────────


async def test_both_miss_error_carries_snapshot(session_factory):
    from app.routers.auth import _validate_single_refresh_token

    seeded = await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
    old_jti, sid = decode_refresh_jti_sid(token)
    # Rotate old_jti out and age the grace so the validator both-misses.
    state_db._rotate(old_jti, "successor-jti", sid, seeded["user_id"], 3600)
    expire_grace(sid)

    async with session_factory() as db:
        with pytest.raises(RefreshBothMissError) as ei:
            await _validate_single_refresh_token(token, db)
    err = ei.value
    assert err.jti == old_jti
    assert err.sid == sid
    assert err.user_id == seeded["user_id"]
    assert err.user_email == "alice@example.com"
    assert err.user_org_id == seeded["org_id"]


# ═════════════════════════════════════════════════════════════════════════
# 11. Concurrency — exactly-once reuse under concurrent presentation
# ═════════════════════════════════════════════════════════════════════════


async def test_concurrent_both_miss_reuse_is_exactly_once(
    session_factory, monkeypatch, _spy_no_notifications
):
    """N concurrent ``/refresh`` calls replaying the SAME stale (past-grace)
    member jti produce EXACTLY ONE reuse+revoke and EXACTLY ONE audit
    write; the N-1 losers classify ``unknown``.

    This locks the exactly-once property that is the whole reason the
    revoke runs inside the family-row lock: concurrent callers serialize on
    it (BEGIN IMMEDIATE on the SQLite test engine), the first consumes the
    family, the rest see no family.

    Exactly-once is asserted at the two in-process control points: the
    reuse-wrapper outcomes (exactly one ``reused``, the rest ``unknown``)
    and the number of times the audit writer is invoked (exactly one). We
    do NOT read the persisted ``auth.session.reuse_detected`` row back:
    the reuse audit uses a SEPARATE ``record_audit_event`` session, and
    under N-way concurrency the test harness's single shared in-memory
    SQLite connection (``StaticPool``) makes that cross-session read
    unreliable. Production uses a per-request pooled connection, so this
    is purely a harness artifact; the audit-writer call count is the
    faithful deterministic proxy for "exactly one row".
    """
    from app.routers import auth as auth_module

    outcomes: list[tuple] = []
    real_wrapper = state_db.session_detect_reuse_and_revoke

    async def _spy_wrapper(jti, sid):
        result = await real_wrapper(jti, sid)
        outcomes.append(result)
        return result

    monkeypatch.setattr(
        state_db, "session_detect_reuse_and_revoke", _spy_wrapper
    )

    audit_calls = {"n": 0}
    real_record = auth_module._record_session_reuse_detected

    async def _spy_record(*args, **kwargs):
        audit_calls["n"] += 1
        return await real_record(*args, **kwargs)

    monkeypatch.setattr(
        auth_module, "_record_session_reuse_detected", _spy_record
    )

    await _seed_user(session_factory)
    app = _make_app(session_factory)

    with TestClient(app) as client:
        token = _login(client)
        old_jti, sid = decode_refresh_jti_sid(token)
        # Rotate once so old_jti becomes a consumed family member.
        set_refresh_cookie(client, token)
        assert client.post("/api/v1/auth/refresh").status_code == 200
    # Past leeway: age the grace out so old_jti both-misses => reuse.
    expire_grace(sid)
    assert old_jti in state_jtis(sid)

    n = 5
    async with _httpx_app_client(app) as ac:
        set_refresh_cookie(ac, token)

        async def _do_refresh():
            return await ac.post("/api/v1/auth/refresh")

        tasks = [asyncio.create_task(_do_refresh()) for _ in range(n)]
        results = await asyncio.gather(*tasks)

    # Every presentation of a both-miss stale jti terminates in a 401.
    assert [r.status_code for r in results] == [401] * n, (
        [r.status_code for r in results]
    )
    # All N callers ran the detection exactly once each.
    assert len(outcomes) == n, outcomes
    # EXACTLY ONE ``reused`` (with the full family size), the rest ``unknown``.
    reused = [o for o in outcomes if o[0] == state_db.SESSION_REUSE_REUSED]
    assert len(reused) == 1, f"expected exactly one reused outcome, got {outcomes}"
    assert reused[0] == (state_db.SESSION_REUSE_REUSED, 2)
    losers = [o for o in outcomes if o[0] != state_db.SESSION_REUSE_REUSED]
    assert all(o == (state_db.SESSION_REUSE_UNKNOWN,) for o in losers), losers
    # EXACTLY ONE audit write attempted (audit is emitted iff ``reused``).
    assert audit_calls["n"] == 1, audit_calls
    # Family consumed exactly once.
    assert state_family(sid) is None
    # NO email / NO in-app notification on the reuse path.
    assert _spy_no_notifications["dispatch"] == 0
    assert _spy_no_notifications["security_email"] == 0


# ═════════════════════════════════════════════════════════════════════════
# 12. Every non-both-miss 401 leaves reuse detection uncalled
# ═════════════════════════════════════════════════════════════════════════


async def _case_iat_before_cutoff(client, session_factory, seeded):
    """A token issued before the user's session cutoff (post logout /
    password change) — a generic 401, primary present, never a both-miss."""
    token = _login(client)
    _jti, sid = decode_refresh_jti_sid(token)
    async with session_factory() as db:
        user = await db.get(User, seeded["user_id"])
        # Cutoff strictly after the token's iat => iat_before_cutoff.
        user.sessions_invalidated_at = datetime.now(timezone.utc) + timedelta(
            minutes=1
        )
        await db.commit()
    return token, sid


async def _case_missing_jti_or_sid(client, session_factory, seeded):
    """A legacy refresh JWT stripped of its jti/sid claims (pre-PR-2)."""
    import jwt as _jwt

    login_token = _login(client)
    _jti, sid = decode_refresh_jti_sid(login_token)
    raw = create_refresh_token(seeded["user_id"], ttl_seconds=3600)[0]
    payload = _jwt.decode(
        raw, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
    )
    payload.pop("jti", None)
    payload.pop("sid", None)
    legacy = _jwt.encode(
        payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm
    )
    return legacy, sid


async def _case_row_binding_mismatch(client, session_factory, seeded):
    """The JWT carries the live head jti but another sid, so the stored
    family's sid mismatches — the jti is the head, so the grace fallback
    never runs; not a both-miss."""
    token = _login(client)
    jti, sid = decode_refresh_jti_sid(token)
    mismatched, _, _ = create_refresh_token(
        seeded["user_id"], sid="wrong-sid-not-the-jwt-sid", jti=jti
    )
    return mismatched, sid


async def _case_forged_signature(client, session_factory, seeded):
    """A structurally valid refresh JWT with a tampered signature —
    fails decode entirely (``invalid_token_decode``)."""
    login_token = _login(client)
    _jti, sid = decode_refresh_jti_sid(login_token)
    forged = login_token.rsplit(".", 1)[0] + ".dGFtcGVyZWRfc2ln"
    return forged, sid


@pytest.mark.parametrize(
    "builder",
    [
        _case_iat_before_cutoff,
        _case_missing_jti_or_sid,
        _case_row_binding_mismatch,
        _case_forged_signature,
    ],
    ids=[
        "iat_before_cutoff",
        "missing_jti_or_sid",
        "row_binding_mismatch",
        "forged_signature",
    ],
)
async def test_non_both_miss_401_never_reaches_reuse_detection(
    session_factory, monkeypatch, builder
):
    """Only the both-miss (neither head nor graced) 401 is a reuse
    candidate. Every OTHER terminal 401 must leave the reuse wrapper
    completely uncalled and the family intact."""
    calls = {"n": 0}
    real = state_db.session_detect_reuse_and_revoke

    async def _spy(jti, sid):
        calls["n"] += 1
        return await real(jti, sid)

    monkeypatch.setattr(state_db, "session_detect_reuse_and_revoke", _spy)

    seeded = await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        present_token, login_sid = await builder(
            client, session_factory, seeded
        )
        set_refresh_cookie(client, present_token)
        res = client.post("/api/v1/auth/refresh")

    assert res.status_code == 401, res.text
    # The reuse wrapper was NEVER invoked.
    assert calls["n"] == 0
    # The login family still exists (nothing revoked it).
    assert state_family(login_sid) is not None
    reuse = await _list_audit(session_factory, "auth.session.reuse_detected")
    assert reuse == []


# ═════════════════════════════════════════════════════════════════════════
# 13. Absolute-lifetime expiry precedes / never masquerades as reuse
# ═════════════════════════════════════════════════════════════════════════


async def test_absolute_lifetime_expiry_is_not_reuse(
    session_factory, monkeypatch
):
    """A token PAST the 30-day absolute lifetime — but as the LIVE head,
    with correct binding and family membership — must 401 with
    ``SESSION_EXPIRED_DETAIL`` and NEVER reach the reuse wrapper. Proves an
    expired-but-legitimate head token is not mistaken for a replayed
    exfiltrated cookie."""
    calls = {"n": 0}
    real = state_db.session_detect_reuse_and_revoke

    async def _spy(jti, sid):
        calls["n"] += 1
        return await real(jti, sid)

    monkeypatch.setattr(state_db, "session_detect_reuse_and_revoke", _spy)

    seeded = await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        login_token = _login(client)
    login_jti, sid = decode_refresh_jti_sid(login_token)

    # Mint a token on the SAME family whose session_created_at predates the
    # 30-day absolute ceiling and make it the family head, so every earlier
    # validator check passes and it reaches the absolute-lifetime gate.
    aged_start = datetime.now(timezone.utc) - timedelta(days=31)
    aged_token, aged_jti, _ = create_refresh_token(
        seeded["user_id"], sid=sid, session_created_at=aged_start
    )
    state_db._rotate(login_jti, aged_jti, sid, seeded["user_id"], 3600)

    with TestClient(app) as client:
        set_refresh_cookie(client, aged_token)
        res = client.post("/api/v1/auth/refresh")

    assert res.status_code == 401, res.text
    assert res.json()["detail"] == SESSION_EXPIRED_DETAIL
    assert calls["n"] == 0
    assert state_family(sid) is not None
    reuse = await _list_audit(session_factory, "auth.session.reuse_detected")
    assert reuse == []
