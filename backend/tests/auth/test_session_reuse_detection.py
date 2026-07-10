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

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

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
from app.database import get_db
from app.deps import get_session_factory
from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers.auth import (
    LEGACY_REFRESH_COOKIE_PATH,
    RefreshBothMissError,
    router as auth_router,
)
from app.security import decode_refresh_jti_sid, hash_password

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
