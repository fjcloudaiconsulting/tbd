"""Refresh-token REUSE detection — fail-safe family revoke past grace.

Pins the security contract added on top of the PR 3 rotation/grace model:

When a refresh token that was rotated PAST the 30s grace/leeway window is
presented to ``/refresh``, treat it as reuse of an exfiltrated cookie and
revoke the WHOLE session family (fail-safe). Audit only, NO email / NO
in-app notification.

Load-bearing separations verified here:
  * ``/verify`` NEVER revokes (read-only) — a both-miss on /verify is a
    plain 401, family intact.
  * The four OTHER 401 reasons that share the "Session has been
    invalidated" detail (iat-cutoff / missing-claim / binding-mismatch /
    family-member-missing) NEVER reach the reuse Lua.
  * Within the grace window a stale jti is a benign catch-up, not reuse.
  * Detection + revoke is exactly-once (idempotent) => exactly one audit.
  * Redis unreachable => 503, never a revoke-on-uncertainty.

Concurrency tests use ``asyncio.gather`` + the fake's ``asyncio.Event``
barrier, NEVER ``asyncio.sleep``.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app import redis_client
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

from tests.conftest import set_refresh_cookie


PASSWORD = "starting-password-1"


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


def _family_key(sid: str) -> str:
    return f"auth:session:by_sid:{sid}"


# ── 1. Reuse past grace revokes the entire family + audits, no email ─────────


async def test_reuse_past_grace_revokes_family(
    session_factory, fake_redis, _spy_no_notifications
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

        # Simulate rotation PAST the leeway: drop old_jti's grace key.
        del fake_redis._kv[f"auth:session:grace:{old_jti}"]
        # Sanity: family still holds both jtis, winner primary is live.
        assert old_jti in fake_redis._sets[_family_key(sid)]
        assert f"auth:session:{winner_jti}" in fake_redis._kv

        # Replay the exfiltrated old cookie past grace => REUSE.
        set_refresh_cookie(client, token)
        res = client.post("/api/v1/auth/refresh")

    assert res.status_code == 401, res.text
    assert res.json()["detail"] == "Session has been invalidated"

    # Whole family gone: family set + every primary + every grace key.
    assert _family_key(sid) not in fake_redis._sets
    assert f"auth:session:{winner_jti}" not in fake_redis._kv
    assert f"auth:session:{old_jti}" not in fake_redis._kv
    assert f"auth:session:grace:{winner_jti}" not in fake_redis._kv

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


async def test_within_leeway_is_benign(session_factory, fake_redis):
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        old_jti, sid = decode_refresh_jti_sid(token)

        set_refresh_cookie(client, token)
        r1 = client.post("/api/v1/auth/refresh")
        assert r1.status_code == 200
        # Grace key alive (within leeway).
        assert f"auth:session:grace:{old_jti}" in fake_redis._kv

        # Replay old cookie WHILE grace is alive => grace catch-up (200).
        set_refresh_cookie(client, token)
        res = client.post("/api/v1/auth/refresh")

    assert res.status_code == 200, res.text
    # Family intact.
    assert _family_key(sid) in fake_redis._sets
    reuse = await _list_audit(session_factory, "auth.session.reuse_detected")
    assert reuse == []
    grace = await _list_audit(session_factory, "auth.session.grace_accept")
    assert len(grace) == 1


# ── 3. Garbage jti (not a family member) => plain 401, no revoke ─────────────


async def test_garbage_jti_no_revoke(session_factory, fake_redis):
    from app.security import create_refresh_token

    seeded = await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        _old_jti, sid = decode_refresh_jti_sid(token)

        # Mint a JWT that decodes fine and passes user/iat/claim checks,
        # carrying the LIVE session's sid but a jti that was never issued
        # (no Redis primary, no grace, not a family member).
        bogus_token, bogus_jti, _sid = create_refresh_token(
            seeded["user_id"], sid=sid, jti="never-issued-jti"
        )
        assert bogus_jti not in fake_redis._sets[_family_key(sid)]

        set_refresh_cookie(client, bogus_token)
        res = client.post("/api/v1/auth/refresh")

    assert res.status_code == 401, res.text
    assert res.json()["detail"] == "Session has been invalidated"
    # Family untouched: the real login jti chain is intact.
    assert _family_key(sid) in fake_redis._sets
    reuse = await _list_audit(session_factory, "auth.session.reuse_detected")
    assert reuse == []


# ── 4. /verify NEVER revokes on a both-miss token (critical) ─────────────────


async def test_verify_never_revokes_on_both_miss(session_factory, fake_redis):
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

        # Past leeway: drop grace. old_jti is now a both-miss.
        del fake_redis._kv[f"auth:session:grace:{old_jti}"]

        # /verify with the both-miss cookie.
        set_refresh_cookie(client, token)
        res = client.post("/api/v1/auth/verify")

    assert res.status_code == 401, res.text
    assert res.json()["detail"] == "Session has been invalidated"
    # /verify never emits Set-Cookie.
    assert _canonical_refresh_cookie(res.headers) is None
    # CRITICAL: family fully intact, winner primary alive, NO reuse audit.
    assert _family_key(sid) in fake_redis._sets
    assert old_jti in fake_redis._sets[_family_key(sid)]
    assert f"auth:session:{winner_jti}" in fake_redis._kv
    reuse = await _list_audit(session_factory, "auth.session.reuse_detected")
    assert reuse == []


# ── 5. Non-both-miss 401 (binding mismatch) never reaches the reuse Lua ──────


async def test_non_both_miss_401_does_not_revoke(
    session_factory, fake_redis, monkeypatch
):
    # Spy: the reuse Lua wrapper must NOT be invoked for a non-both-miss 401.
    calls = {"n": 0}
    real = redis_client.session_detect_reuse_and_revoke

    async def _spy(jti, sid):
        calls["n"] += 1
        return await real(jti, sid)

    monkeypatch.setattr(redis_client, "session_detect_reuse_and_revoke", _spy)

    seeded = await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        jti, sid = decode_refresh_jti_sid(token)

        # Corrupt the primary row so its stored sid mismatches the JWT's
        # sid => row_binding_mismatch (a generic 401, NOT a both-miss:
        # the primary key is present, so the grace fallback never runs).
        fake_redis._kv[f"auth:session:{jti}"] = json.dumps(
            {"user_id": seeded["user_id"], "sid": "wrong-sid-not-the-jwt-sid"}
        )

        set_refresh_cookie(client, token)
        res = client.post("/api/v1/auth/refresh")

    assert res.status_code == 401, res.text
    assert res.json()["detail"] == "Session has been invalidated"
    # The reuse Lua was NEVER called.
    assert calls["n"] == 0
    # Family intact, no reuse audit.
    assert _family_key(sid) in fake_redis._sets
    reuse = await _list_audit(session_factory, "auth.session.reuse_detected")
    assert reuse == []


# ── 6. Idempotent: two sequential replays revoke once, one audit ─────────────


async def test_reuse_is_idempotent_single_audit(session_factory, fake_redis):
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        old_jti, sid = decode_refresh_jti_sid(token)

        set_refresh_cookie(client, token)
        assert client.post("/api/v1/auth/refresh").status_code == 200
        del fake_redis._kv[f"auth:session:grace:{old_jti}"]

        # First replay => reuse + revoke.
        set_refresh_cookie(client, token)
        first = client.post("/api/v1/auth/refresh")
        assert first.status_code == 401
        assert _family_key(sid) not in fake_redis._sets

        # Second replay of the SAME stale jti => family already gone,
        # SISMEMBER == 0 => "unknown" => plain 401, no second revoke/audit.
        set_refresh_cookie(client, token)
        second = client.post("/api/v1/auth/refresh")
        assert second.status_code == 401

    reuse = await _list_audit(session_factory, "auth.session.reuse_detected")
    assert len(reuse) == 1, f"expected exactly 1 reuse_detected, got {len(reuse)}"


# ── 7. Redis error during detection => 503, no revoke-on-uncertainty ─────────


async def test_reuse_redis_error_returns_503(
    session_factory, fake_redis, monkeypatch
):
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        old_jti, sid = decode_refresh_jti_sid(token)

        set_refresh_cookie(client, token)
        assert client.post("/api/v1/auth/refresh").status_code == 200
        del fake_redis._kv[f"auth:session:grace:{old_jti}"]

        async def _boom(jti, sid):
            raise RedisConnectionError("valkey down")

        monkeypatch.setattr(
            redis_client, "session_detect_reuse_and_revoke", _boom
        )

        set_refresh_cookie(client, token)
        res = client.post("/api/v1/auth/refresh")

    assert res.status_code == 503, res.text
    # Family NOT revoked on uncertainty.
    assert _family_key(sid) in fake_redis._sets
    reuse = await _list_audit(session_factory, "auth.session.reuse_detected")
    assert reuse == []


# ── 8. Direct wrapper classification (live / grace / unknown / reused) ───────


async def test_wrapper_classifications(fake_redis):
    sid = "sid-classify"
    family = _family_key(sid)

    # live: primary present.
    fake_redis._kv["auth:session:jlive"] = json.dumps({"user_id": 1, "sid": sid})
    assert await redis_client.session_detect_reuse_and_revoke("jlive", sid) == (
        "live",
    )

    # grace: only the grace key present.
    fake_redis._kv["auth:session:grace:jgrace"] = json.dumps(
        {"user_id": 1, "sid": sid, "successor_jti": "x"}
    )
    assert await redis_client.session_detect_reuse_and_revoke("jgrace", sid) == (
        "grace",
    )

    # unknown: not a member, no primary, no grace.
    assert await redis_client.session_detect_reuse_and_revoke("jnope", sid) == (
        "unknown",
    )

    # reused: consumed family member, primary + grace gone.
    fake_redis._sets[family].update({"jold", "jnew"})
    fake_redis._kv["auth:session:jnew"] = json.dumps({"user_id": 1, "sid": sid})
    result = await redis_client.session_detect_reuse_and_revoke("jold", sid)
    assert result == ("reused", 2)
    assert family not in fake_redis._sets
    assert "auth:session:jnew" not in fake_redis._kv


# ── 9. RefreshBothMissError carries the snapshot needed for audit ────────────


async def test_both_miss_error_carries_snapshot(session_factory, fake_redis):
    from app.routers.auth import _validate_single_refresh_token

    seeded = await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
    old_jti, sid = decode_refresh_jti_sid(token)
    # Wipe primary + grace so the validator both-misses.
    fake_redis._kv.pop(f"auth:session:{old_jti}", None)
    fake_redis._kv.pop(f"auth:session:grace:{old_jti}", None)

    async with session_factory() as db:
        with pytest.raises(RefreshBothMissError) as ei:
            await _validate_single_refresh_token(token, db)
    err = ei.value
    assert err.jti == old_jti
    assert err.sid == sid
    assert err.user_id == seeded["user_id"]
    assert err.user_email == "alice@example.com"
    assert err.user_org_id == seeded["org_id"]


# ── Local key helpers for the direct-Redis tests below ──────────────────────


def _primary_key(jti: str) -> str:
    return f"auth:session:{jti}"


def _grace_key(jti: str) -> str:
    return f"auth:session:grace:{jti}"


def _uid() -> str:
    return uuid.uuid4().hex


# ═════════════════════════════════════════════════════════════════════════
# 10. REAL-Redis integration — execute the SHIPPED DETECT_REUSE_LUA
# ═════════════════════════════════════════════════════════════════════════
#
# The autouse fake in ``tests/conftest.py`` REIMPLEMENTS DETECT_REUSE_LUA
# in Python (``_eval_detect_reuse``), so every fake-backed test above
# proves the ROUTER contract but NEVER runs the real Lua string. These
# tests close that gap permanently: they point ``redis_client.get_client``
# at a REAL client built by the shipped ``_build_auth_redis_client`` (the
# monkeypatch runs after the autouse fake, so it wins) and drive the public
# ``session_detect_reuse_and_revoke`` wrapper end to end against a real
# Redis/Valkey. A future prefix typo, guard-order swap or member-loop bug
# in the Lua would now fail CI instead of hiding behind the fake.
#
# Skipped (never failed) when no real Redis is reachable, so a plain unit
# run with no server stays green.


@pytest_asyncio.fixture
async def real_redis(monkeypatch):
    """Real Redis/Valkey client for executing the shipped Lua string.

    Bypasses the autouse fake by rebuilding the production client from
    ``settings.redis_url`` and repointing ``redis_client.get_client`` at
    it (so ``require_client()`` inside the wrapper resolves to the real
    server). Skips when no real Redis is reachable.

    Seeded keys use a short TTL so any key a failing assertion leaves
    behind self-expires — no cross-test contamination in the shared
    stack.
    """
    from redis.exceptions import RedisError

    if not settings.redis_url:
        pytest.skip("no settings.redis_url configured for the real-Redis test")
    client = redis_client._build_auth_redis_client(settings.redis_url)
    try:
        await client.ping()
    except (RedisError, OSError) as exc:  # pragma: no cover - infra gate
        await client.aclose()
        pytest.skip(f"real Redis not reachable at {settings.redis_url!r}: {exc}")
    monkeypatch.setattr(redis_client, "get_client", lambda: client)
    monkeypatch.setattr(redis_client, "_client", client, raising=False)
    try:
        yield client
    finally:
        await client.aclose()


async def _seed_member(client, sid: str, jti: str, ex: int = 300) -> None:
    await client.sadd(_family_key(sid), jti)
    await client.expire(_family_key(sid), ex)


async def _seed_primary(client, sid: str, jti: str, ex: int = 300) -> None:
    await client.set(
        _primary_key(jti),
        json.dumps({"user_id": 1, "sid": sid}),
        ex=ex,
    )


async def _seed_grace(client, sid: str, jti: str, ex: int = 300) -> None:
    await client.set(
        _grace_key(jti),
        json.dumps({"user_id": 1, "sid": sid, "successor_jti": "x"}),
        ex=ex,
    )


async def test_real_lua_primary_present_returns_live(real_redis):
    """Primary key present => ``('live',)``, family untouched."""
    sid, jti = _uid(), _uid()
    await _seed_member(real_redis, sid, jti)
    await _seed_primary(real_redis, sid, jti)

    result = await redis_client.session_detect_reuse_and_revoke(jti, sid)

    assert result == ("live",)
    assert await real_redis.exists(_family_key(sid)) == 1
    assert await real_redis.sismember(_family_key(sid), jti)


async def test_real_lua_grace_present_returns_grace(real_redis):
    """Primary gone + grace present => ``('grace',)``, family untouched."""
    sid, jti = _uid(), _uid()
    await _seed_member(real_redis, sid, jti)
    await _seed_grace(real_redis, sid, jti)  # no primary

    result = await redis_client.session_detect_reuse_and_revoke(jti, sid)

    assert result == ("grace",)
    assert await real_redis.exists(_family_key(sid)) == 1
    assert await real_redis.sismember(_family_key(sid), jti)


async def test_real_lua_non_member_returns_unknown_no_partial_revoke(real_redis):
    """Both gone + jti NOT a member => ``('unknown',)``; the family and
    every other member survive (NO partial revoke)."""
    sid = _uid()
    member_a, member_b, stranger = _uid(), _uid(), _uid()
    await _seed_member(real_redis, sid, member_a)
    await _seed_member(real_redis, sid, member_b)
    await _seed_primary(real_redis, sid, member_b)  # a live head coexists

    result = await redis_client.session_detect_reuse_and_revoke(stranger, sid)

    assert result == ("unknown",)
    # Family + all other members intact — the unknown branch must not touch
    # a single key.
    assert await real_redis.exists(_family_key(sid)) == 1
    assert await real_redis.sismember(_family_key(sid), member_a)
    assert await real_redis.sismember(_family_key(sid), member_b)
    assert await real_redis.exists(_primary_key(member_b)) == 1


async def test_real_lua_reused_revokes_entire_family(real_redis):
    """Both gone + jti IS a member (with two other members) => the whole
    family (set + every member's primary AND grace) is DELeted, and the
    wrapper returns ``('reused', 3)``."""
    sid = _uid()
    stale, head, other = _uid(), _uid(), _uid()
    for m in (stale, head, other):
        await _seed_member(real_redis, sid, m)
    # ``stale`` is a consumed member (no primary, no grace). Give the two
    # other members BOTH a primary and a grace key so we can prove the
    # member loop deletes every key shape.
    await _seed_primary(real_redis, sid, head)
    await _seed_primary(real_redis, sid, other)
    await _seed_grace(real_redis, sid, head)
    await _seed_grace(real_redis, sid, other)

    result = await redis_client.session_detect_reuse_and_revoke(stale, sid)

    assert result == ("reused", 3)
    # The family set is gone AND every member's primary + grace key is gone.
    assert await real_redis.exists(_family_key(sid)) == 0
    for m in (stale, head, other):
        assert await real_redis.exists(_primary_key(m)) == 0, m
        assert await real_redis.exists(_grace_key(m)) == 0, m


async def test_real_lua_second_call_is_idempotent_unknown(real_redis):
    """A second call on the same jti/sid after the revoke => ``('unknown',)``
    (the family is gone => ``SISMEMBER`` == 0). No second revoke — this is
    the property the inlined-in-Lua revoke exists to guarantee."""
    sid = _uid()
    stale, head = _uid(), _uid()
    await _seed_member(real_redis, sid, stale)
    await _seed_member(real_redis, sid, head)
    await _seed_primary(real_redis, sid, head)

    first = await redis_client.session_detect_reuse_and_revoke(stale, sid)
    assert first == ("reused", 2)
    assert await real_redis.exists(_family_key(sid)) == 0

    second = await redis_client.session_detect_reuse_and_revoke(stale, sid)
    assert second == ("unknown",)
    assert await real_redis.exists(_family_key(sid)) == 0


# ═════════════════════════════════════════════════════════════════════════
# 11. Concurrency — exactly-once reuse under concurrent presentation
# ═════════════════════════════════════════════════════════════════════════


async def test_concurrent_both_miss_reuse_is_exactly_once(
    session_factory, fake_redis, monkeypatch, _spy_no_notifications
):
    """N concurrent ``/refresh`` calls replaying the SAME stale (past-grace)
    member jti produce EXACTLY ONE reuse+revoke and EXACTLY ONE audit
    write; the N-1 losers classify ``unknown``.

    This locks the exactly-once property that is the whole reason the
    revoke is inlined in the atomic Lua. The fake's ``eval_barrier`` lands
    all N callers at the detect-reuse script body before any runs, and its
    ``_eval_lock`` then serializes them — the first consumes the family,
    the rest see an empty family. Gated with ``asyncio.Event`` (the fake's
    barrier), never ``asyncio.sleep``.

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
    real_wrapper = redis_client.session_detect_reuse_and_revoke

    async def _spy_wrapper(jti, sid):
        result = await real_wrapper(jti, sid)
        outcomes.append(result)
        return result

    monkeypatch.setattr(
        redis_client, "session_detect_reuse_and_revoke", _spy_wrapper
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
    # Past leeway: drop the grace key so old_jti both-misses => reuse.
    del fake_redis._kv[f"auth:session:grace:{old_jti}"]
    assert old_jti in fake_redis._sets[_family_key(sid)]

    n = 5
    fake_redis.eval_barrier_target = n
    async with _httpx_app_client(app) as ac:
        set_refresh_cookie(ac, token)

        async def _do_refresh():
            return await ac.post("/api/v1/auth/refresh")

        tasks = [asyncio.create_task(_do_refresh()) for _ in range(n)]
        # Deterministically wait until all N coroutines reach the Lua
        # barrier, then release them together. Bound the wait so an unmet
        # barrier (e.g. a future change short-circuits the both-miss path
        # before the Lua) fails fast instead of hanging as a CI timeout.
        await asyncio.wait_for(fake_redis._eval_arrival_event.wait(), timeout=5)
        fake_redis._eval_release_event.set()
        results = await asyncio.gather(*tasks)

    fake_redis.eval_barrier_target = None

    # Every presentation of a both-miss stale jti terminates in a 401.
    assert [r.status_code for r in results] == [401] * n, (
        [r.status_code for r in results]
    )
    # All N callers ran the detect Lua exactly once each.
    assert len(outcomes) == n, outcomes
    # EXACTLY ONE ``reused`` (with the full family size), the rest ``unknown``.
    reused = [o for o in outcomes if o[0] == redis_client.SESSION_REUSE_REUSED]
    assert len(reused) == 1, f"expected exactly one reused outcome, got {outcomes}"
    assert reused[0] == (redis_client.SESSION_REUSE_REUSED, 2)
    losers = [o for o in outcomes if o[0] != redis_client.SESSION_REUSE_REUSED]
    assert all(o == (redis_client.SESSION_REUSE_UNKNOWN,) for o in losers), losers
    # EXACTLY ONE audit write attempted (audit is emitted iff ``reused``).
    assert audit_calls["n"] == 1, audit_calls
    # Family consumed exactly once.
    assert _family_key(sid) not in fake_redis._sets
    # NO email / NO in-app notification on the reuse path.
    assert _spy_no_notifications["dispatch"] == 0
    assert _spy_no_notifications["security_email"] == 0


# ═════════════════════════════════════════════════════════════════════════
# 12. Every non-both-miss 401 leaves the reuse wrapper uncalled
# ═════════════════════════════════════════════════════════════════════════


async def _case_iat_before_cutoff(client, session_factory, fake_redis, seeded):
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


async def _case_missing_jti_or_sid(client, session_factory, fake_redis, seeded):
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


async def _case_row_binding_mismatch(client, session_factory, fake_redis, seeded):
    """Primary row's stored sid mismatches the JWT sid — primary present,
    so the grace fallback never runs; not a both-miss."""
    token = _login(client)
    jti, sid = decode_refresh_jti_sid(token)
    fake_redis._kv[_primary_key(jti)] = json.dumps(
        {"user_id": seeded["user_id"], "sid": "wrong-sid-not-the-jwt-sid"}
    )
    return token, sid


async def _case_family_member_missing(client, session_factory, fake_redis, seeded):
    """Primary present + binding OK, but the jti is no longer a member of
    the family set (logout Round A landed) — a generic 401, not a
    both-miss."""
    token = _login(client)
    jti, sid = decode_refresh_jti_sid(token)
    fake_redis._sets[_family_key(sid)].discard(jti)
    return token, sid


async def _case_forged_signature(client, session_factory, fake_redis, seeded):
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
        _case_family_member_missing,
        _case_forged_signature,
    ],
    ids=[
        "iat_before_cutoff",
        "missing_jti_or_sid",
        "row_binding_mismatch",
        "family_member_missing",
        "forged_signature",
    ],
)
async def test_non_both_miss_401_never_reaches_reuse_lua(
    session_factory, fake_redis, monkeypatch, builder
):
    """Only the both-miss (primary AND grace gone past leeway) 401 is a
    reuse candidate. Every OTHER terminal 401 must leave the reuse wrapper
    completely uncalled and the family intact."""
    calls = {"n": 0}
    real = redis_client.session_detect_reuse_and_revoke

    async def _spy(jti, sid):
        calls["n"] += 1
        return await real(jti, sid)

    monkeypatch.setattr(redis_client, "session_detect_reuse_and_revoke", _spy)

    seeded = await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        present_token, login_sid = await builder(
            client, session_factory, fake_redis, seeded
        )
        set_refresh_cookie(client, present_token)
        res = client.post("/api/v1/auth/refresh")

    assert res.status_code == 401, res.text
    # The reuse Lua wrapper was NEVER invoked.
    assert calls["n"] == 0
    # The login family set still exists (nothing revoked it).
    assert _family_key(login_sid) in fake_redis._sets
    reuse = await _list_audit(session_factory, "auth.session.reuse_detected")
    assert reuse == []


# ═════════════════════════════════════════════════════════════════════════
# 13. Absolute-lifetime expiry precedes / never masquerades as reuse
# ═════════════════════════════════════════════════════════════════════════


async def test_absolute_lifetime_expiry_is_not_reuse(
    session_factory, fake_redis, monkeypatch
):
    """A token PAST the 30-day absolute lifetime — but with a LIVE primary,
    correct binding and family membership — must 401 with
    ``SESSION_EXPIRED_DETAIL`` and NEVER reach the reuse wrapper. Proves an
    expired-but-legitimate head token is not mistaken for a replayed
    exfiltrated cookie."""
    calls = {"n": 0}
    real = redis_client.session_detect_reuse_and_revoke

    async def _spy(jti, sid):
        calls["n"] += 1
        return await real(jti, sid)

    monkeypatch.setattr(redis_client, "session_detect_reuse_and_revoke", _spy)

    seeded = await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        login_token = _login(client)
    _login_jti, sid = decode_refresh_jti_sid(login_token)

    # Mint a token on the SAME family whose session_created_at predates the
    # 30-day absolute ceiling, and give it a live primary + family
    # membership so every earlier validator check passes and it reaches the
    # absolute-lifetime gate.
    aged_start = datetime.now(timezone.utc) - timedelta(days=31)
    aged_token, aged_jti, _ = create_refresh_token(
        seeded["user_id"], sid=sid, session_created_at=aged_start
    )
    fake_redis._kv[_primary_key(aged_jti)] = json.dumps(
        {"user_id": seeded["user_id"], "sid": sid}
    )
    fake_redis._sets[_family_key(sid)].add(aged_jti)

    with TestClient(app) as client:
        set_refresh_cookie(client, aged_token)
        res = client.post("/api/v1/auth/refresh")

    assert res.status_code == 401, res.text
    assert res.json()["detail"] == SESSION_EXPIRED_DETAIL
    assert calls["n"] == 0
    assert _family_key(sid) in fake_redis._sets
    reuse = await _list_audit(session_factory, "auth.session.reuse_detected")
    assert reuse == []


# ═════════════════════════════════════════════════════════════════════════
# 14. Regression: family-set TTL stays in lockstep with the head primary
# ═════════════════════════════════════════════════════════════════════════


async def test_family_ttl_tracks_head_primary_ttl_issue_and_rotate(real_redis):
    """Pin the IMPLICIT invariant the absolute-lifetime safety rests on:
    the family-set TTL is written in lockstep with the head-primary TTL, at
    both ``session_issue`` AND rotation. If a future change ever let the
    family TTL OUTLIVE the head primary, an idle-expired legitimate head
    token could present as ``SISMEMBER == 1`` (primary+grace gone but family
    still alive) and wrongly trigger a family revoke. This regression test
    catches that decoupling. (Test-only, per review — the validator check
    order is intentionally NOT reordered.)"""
    sid, jti = _uid(), _uid()
    ttl = 3600

    await redis_client.session_issue(jti, sid, 1, ttl)
    ttl_primary = await real_redis.ttl(_primary_key(jti))
    ttl_family = await real_redis.ttl(_family_key(sid))
    assert ttl_primary > 0 and ttl_family > 0, (ttl_primary, ttl_family)
    # Allow a small delta for the two SET/EXPIRE ops in the MULTI landing at
    # slightly different whole-second boundaries.
    assert abs(ttl_family - ttl_primary) <= 2, (ttl_family, ttl_primary)

    # After a rotation the invariant must still hold for the NEW head.
    new_jti = _uid()
    rotate_result = await redis_client.session_rotate_lua(jti, new_jti, sid, 1, ttl)
    assert rotate_result == "ok"
    ttl_new_primary = await real_redis.ttl(_primary_key(new_jti))
    ttl_family_after = await real_redis.ttl(_family_key(sid))
    assert ttl_new_primary > 0 and ttl_family_after > 0, (
        ttl_new_primary,
        ttl_family_after,
    )
    assert abs(ttl_family_after - ttl_new_primary) <= 2, (
        ttl_family_after,
        ttl_new_primary,
    )

    # Tidy up (keys otherwise self-expire in an hour).
    await real_redis.delete(
        _family_key(sid),
        _primary_key(jti),
        _primary_key(new_jti),
        _grace_key(jti),
    )
