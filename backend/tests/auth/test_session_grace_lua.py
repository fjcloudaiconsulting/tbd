"""PR 3 — Rotation grace window + Lua rotation + verify fallback tests.

Pins every architect-emphasized risk in
``specs/2026-05-17-backend-session-model.md`` §8 PR 3:

1. Cross-tab race produces exactly one rotation + one grace acceptance
   (the canonical no-double-issue pin — the Lua ``EXISTS old_primary``
   guard is what makes this pass).
2. Replay of an already-rotated jti AFTER the 30s grace window fails.
3. ``jti_collision`` path: under a forced-collision RNG the first Lua
   call returns ``jti_collision``, the router regenerates, the second
   call succeeds; under always-collide RNG the router returns 503
   with the ``auth.session.rotated.failed`` audit row.
4. Grace branch family-set check: if logout deletes the family set
   inside the grace window, the grace branch rejects (architect
   P1.1 — closes the logout-vs-rotation race).
5. ``/verify`` mirrors ``/refresh`` — accepts a grace ticket when the
   family is still alive, rejects when the family is gone.
6. Concurrent rotation produces the correct audit shape: one
   ``auth.session.rotated`` AND one
   ``auth.session.grace_accept {via_already_rotated: true}``.
7. ``sid`` mismatch on the grace branch rejects.
8. Replay-after-logout class — within the 30s grace window, if logout
   has deleted the family set, the grace branch must reject.

Concurrency tests use ``asyncio.gather`` + ``asyncio.Event`` gating,
NEVER ``asyncio.sleep`` — the architect's #1 named concern for flake.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_session_factory
from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers.auth import (
    LEGACY_REFRESH_COOKIE_PATH,
    router as auth_router,
)
from app.security import decode_refresh_jti_sid, hash_password


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
    """Run the FastAPI app under ``httpx.AsyncClient`` so two coroutines
    can hit ``/refresh`` truly concurrently. ``TestClient`` is synchronous
    so it would serialize the two requests."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ── 1. Replay AFTER the grace window (manual expiry) ─────────────────────────


async def test_replay_after_grace_window_returns_401(session_factory, fake_redis):
    """Grace window is 30s. Once both the primary key and the grace key
    are gone, the old jti must 401.

    We can't wait 31s in unit tests; instead we manually delete the
    grace key (equivalent to TTL expiry) after the rotation.
    """
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        old_jti, _sid = decode_refresh_jti_sid(token)

        # First refresh rotates.
        first = client.post("/api/v1/auth/refresh", cookies={"refresh_token": token})
        assert first.status_code == 200
        # Grace key should exist now.
        assert f"auth:session:grace:{old_jti}" in fake_redis._kv

        # Simulate TTL expiry: delete the grace key.
        del fake_redis._kv[f"auth:session:grace:{old_jti}"]

        # Replay the old cookie.
        second = client.post(
            "/api/v1/auth/refresh", cookies={"refresh_token": token}
        )
    assert second.status_code == 401
    assert second.json()["detail"] == "Session has been invalidated"


# ── 2. Cross-tab race — gated concurrent /refresh produces 1 + 1 ─────────────


async def test_concurrent_refresh_one_winner_one_grace(session_factory, fake_redis):
    """Two concurrent ``/refresh`` calls with the same pre-rotation cookie
    produce exactly: one winner (200 + Set-Cookie) and one loser (200,
    no Set-Cookie). Zero 401s.

    Implementation: gate BOTH coroutines at the Lua entry with an
    ``asyncio.Event``, release them simultaneously. The serialization
    lock inside the fake's ``eval`` makes the second call observe the
    winner's writes (primary gone, grace written, family extended)
    and return ``already_rotated``. The router then falls into the
    grace branch and issues an access token only.

    Without the Lua ``EXISTS old_primary`` guard (spec §4.2 check 2),
    both calls would pass ``SISMEMBER`` and both rotate — the test
    would observe two distinct successor jtis. This is the canonical
    no-double-issue pin.
    """
    await _seed_user(session_factory)
    app = _make_app(session_factory)

    # Boot one TestClient to log in (sync), then drive concurrency with
    # httpx.AsyncClient.
    with TestClient(app) as client:
        token = _login(client)
    old_jti, sid = decode_refresh_jti_sid(token)

    # Arm the Lua barrier so BOTH coroutines reach the script body
    # before either runs the guards. Release simultaneously.
    fake_redis.eval_barrier_target = 2

    async with _httpx_app_client(app) as ac:
        async def _do_refresh():
            return await ac.post(
                "/api/v1/auth/refresh", cookies={"refresh_token": token}
            )

        task_a = asyncio.create_task(_do_refresh())
        task_b = asyncio.create_task(_do_refresh())
        # Wait deterministically until both coroutines have arrived at
        # the barrier. No ``asyncio.sleep`` — pure event signaling.
        await fake_redis._eval_arrival_event.wait()
        fake_redis._eval_release_event.set()
        res_a, res_b = await asyncio.gather(task_a, task_b)

    # Reset barrier for any later tests.
    fake_redis.eval_barrier_target = None

    statuses = sorted([res_a.status_code, res_b.status_code])
    assert statuses == [200, 200], (
        f"expected two 200s, got {statuses}: A={res_a.text!r} B={res_b.text!r}"
    )

    cookies_a = _canonical_refresh_cookie(res_a.headers)
    cookies_b = _canonical_refresh_cookie(res_b.headers)
    set_cookies = [c for c in (cookies_a, cookies_b) if c is not None]
    assert len(set_cookies) == 1, (
        f"expected exactly one Set-Cookie (winner), got {len(set_cookies)}: "
        f"A={cookies_a!r} B={cookies_b!r}"
    )

    # Exactly one new jti minted; primary key is in Redis.
    winner_token = _refresh_token_from_set_cookie(set_cookies[0])
    winner_jti, winner_sid = decode_refresh_jti_sid(winner_token)
    assert winner_sid == sid
    assert winner_jti != old_jti
    assert f"auth:session:{winner_jti}" in fake_redis._kv
    # Grace key for old_jti is alive.
    assert f"auth:session:grace:{old_jti}" in fake_redis._kv
    # Old primary is gone.
    assert f"auth:session:{old_jti}" not in fake_redis._kv


# ── 3. Audit shape on the cross-tab race ─────────────────────────────────────


async def test_concurrent_refresh_emits_one_rotated_and_one_grace_accept(
    session_factory, fake_redis
):
    """The race in the previous test must emit BOTH audit events:
    one ``auth.session.rotated`` (winner) AND one
    ``auth.session.grace_accept {via_already_rotated: true}`` (loser)."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)

    with TestClient(app) as client:
        token = _login(client)
    old_jti, sid = decode_refresh_jti_sid(token)

    fake_redis.eval_barrier_target = 2

    async with _httpx_app_client(app) as ac:
        async def _do_refresh():
            return await ac.post(
                "/api/v1/auth/refresh", cookies={"refresh_token": token}
            )

        task_a = asyncio.create_task(_do_refresh())
        task_b = asyncio.create_task(_do_refresh())
        await fake_redis._eval_arrival_event.wait()
        fake_redis._eval_release_event.set()
        await asyncio.gather(task_a, task_b)

    fake_redis.eval_barrier_target = None

    rotated = await _list_audit(session_factory, "auth.session.rotated")
    grace = await _list_audit(session_factory, "auth.session.grace_accept")
    assert len(rotated) == 1, f"expected 1 rotated event, got {len(rotated)}"
    assert len(grace) == 1, f"expected 1 grace_accept event, got {len(grace)}"
    assert grace[0].detail["via_already_rotated"] is True
    assert grace[0].detail["sid"] == sid
    assert grace[0].detail["old_jti"] == old_jti


# ── 4. /verify accepts a grace ticket (family alive) ────────────────────────


async def test_verify_accepts_grace_ticket_when_family_alive(
    session_factory, fake_redis
):
    """``/verify`` mirrors ``/refresh`` grace fallback (spec §5.2)."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        old_jti, sid = decode_refresh_jti_sid(token)

        # Rotate once to land the grace key.
        r1 = client.post("/api/v1/auth/refresh", cookies={"refresh_token": token})
        assert r1.status_code == 200
        assert f"auth:session:grace:{old_jti}" in fake_redis._kv

        # /verify with the OLD cookie — primary gone, grace alive, family alive.
        res = client.post("/api/v1/auth/verify", cookies={"refresh_token": token})
    assert res.status_code == 200, res.text
    # Invariant: no Set-Cookie from /verify.
    assert _canonical_refresh_cookie(res.headers) is None


# ── 5. /verify rejects grace ticket when family deleted ─────────────────────


async def test_verify_rejects_grace_ticket_when_family_deleted(
    session_factory, fake_redis
):
    """Grace key alive BUT the family set has been deleted (concurrent
    logout) — ``/verify`` must reject. Without the family-set check
    ``/verify`` would accept while ``/refresh`` would reject — exactly
    the inconsistency the architect called out."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        old_jti, sid = decode_refresh_jti_sid(token)

        r1 = client.post("/api/v1/auth/refresh", cookies={"refresh_token": token})
        assert r1.status_code == 200

        # Simulate concurrent logout: wipe the family set.
        del fake_redis._sets[f"auth:session:by_sid:{sid}"]

        res = client.post("/api/v1/auth/verify", cookies={"refresh_token": token})
    assert res.status_code == 401


# ── 6. /refresh grace branch rejects when family deleted ────────────────────


async def test_refresh_grace_branch_rejects_when_family_deleted(
    session_factory, fake_redis
):
    """Architect P1.1 — even within the 30s grace window, if logout has
    deleted the family set the grace branch must reject. Replay-after-
    logout class."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        old_jti, sid = decode_refresh_jti_sid(token)

        # Rotate so old_jti has only a grace key.
        r1 = client.post("/api/v1/auth/refresh", cookies={"refresh_token": token})
        assert r1.status_code == 200
        assert f"auth:session:grace:{old_jti}" in fake_redis._kv

        # Concurrent logout: wipe the family set.
        del fake_redis._sets[f"auth:session:by_sid:{sid}"]

        res = client.post(
            "/api/v1/auth/refresh", cookies={"refresh_token": token}
        )
    assert res.status_code == 401, res.text
    assert res.json()["detail"] == "Session has been invalidated"


# ── 7. /refresh grace branch rejects when sid in grace row differs ──────────


async def test_refresh_grace_branch_rejects_on_sid_mismatch(
    session_factory, fake_redis
):
    """Defence against an attacker minting a JWT with someone else's
    jti + their own sid. The grace row's stored sid must match the
    JWT's sid claim."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        old_jti, sid = decode_refresh_jti_sid(token)

        r1 = client.post("/api/v1/auth/refresh", cookies={"refresh_token": token})
        assert r1.status_code == 200
        # Corrupt the grace row so its sid mismatches the JWT's sid.
        fake_redis._kv[f"auth:session:grace:{old_jti}"] = json.dumps(
            {"user_id": 1, "sid": "deadbeef-not-the-real-sid", "successor_jti": "x"}
        )

        res = client.post(
            "/api/v1/auth/refresh", cookies={"refresh_token": token}
        )
    assert res.status_code == 401


# ── 8. jti_collision: forced single-collision RNG retries and succeeds ──────


async def test_jti_collision_retries_and_succeeds(
    session_factory, fake_redis, monkeypatch
):
    """Forced-collision RNG returns the SAME jti for two successive calls.
    First Lua call returns ``jti_collision`` (the NX guard fires because
    that jti is already a live primary). Router regenerates and the
    second attempt succeeds. Audit ``auth.session.rotated`` emitted ONCE.

    We rig the collision by patching ``secrets.token_urlsafe`` inside
    ``app.security`` so the FIRST two refresh-token mints get the same
    jti, then the third (the router's retry) gets a fresh one.
    """
    import secrets as _secrets

    real_token_urlsafe = _secrets.token_urlsafe
    # First call to create_refresh_token returns the colliding jti
    # (first Lua attempt -> jti_collision). Second call gets a fresh
    # value from the real RNG so the retry succeeds.
    sequence = iter(["collide-with-existing-primary"])

    def _patched_token_urlsafe(n: int = 16) -> str:
        try:
            return next(sequence)
        except StopIteration:
            return real_token_urlsafe(n)

    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)

    # Seed a primary key that will collide with the first patched jti
    # we hand to the rotate call.
    fake_redis._kv["auth:session:collide-with-existing-primary"] = json.dumps(
        {"user_id": 999999, "sid": "unrelated"}
    )

    monkeypatch.setattr(
        "app.security.secrets.token_urlsafe", _patched_token_urlsafe
    )

    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/refresh", cookies={"refresh_token": token}
        )
    assert res.status_code == 200, res.text
    assert _canonical_refresh_cookie(res.headers) is not None

    rotated = await _list_audit(session_factory, "auth.session.rotated")
    assert len(rotated) == 1, f"expected 1 rotated event, got {len(rotated)}"
    failed = await _list_audit(session_factory, "auth.session.rotated.failed")
    assert failed == []


# ── 9. jti_collision: always-collide RNG => 503 + audit ─────────────────────


async def test_jti_collision_double_failure_returns_503(
    session_factory, fake_redis, monkeypatch
):
    """If the RNG collides on BOTH attempts the router returns 503 and
    emits ``auth.session.rotated.failed`` exactly once."""

    def _always_collide(n: int = 16) -> str:
        return "collide-with-existing-primary"

    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)

    fake_redis._kv["auth:session:collide-with-existing-primary"] = json.dumps(
        {"user_id": 999999, "sid": "unrelated"}
    )

    monkeypatch.setattr(
        "app.security.secrets.token_urlsafe", _always_collide
    )

    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/refresh", cookies={"refresh_token": token}
        )
    assert res.status_code == 503, res.text
    assert _canonical_refresh_cookie(res.headers) is None

    failed = await _list_audit(session_factory, "auth.session.rotated.failed")
    assert len(failed) == 1, f"expected 1 rotated.failed event, got {len(failed)}"
    rotated = await _list_audit(session_factory, "auth.session.rotated")
    assert rotated == []


# ── 10. Direct grace path (the typical cross-tab race after the fact) ───────


async def test_refresh_direct_grace_path_no_setcookie_and_audit(
    session_factory, fake_redis
):
    """The "boring" cross-tab race: tab A rotates first, tab B's
    ``/refresh`` arrives later still carrying the old cookie. The
    primary is already gone, the grace key is alive, the family set
    is alive — the validator hands us ``redis_state == "grace"`` and
    the router returns access-only without entering Lua. Audit:
    ``auth.session.grace_accept {via_already_rotated: false}``.
    """
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        old_jti, sid = decode_refresh_jti_sid(token)

        r1 = client.post("/api/v1/auth/refresh", cookies={"refresh_token": token})
        assert r1.status_code == 200

        # At this point primary {old_jti} is gone, grace alive, family alive.
        # Tab B replays the old cookie.
        res = client.post(
            "/api/v1/auth/refresh", cookies={"refresh_token": token}
        )

    assert res.status_code == 200, res.text
    assert _canonical_refresh_cookie(res.headers) is None, (
        "grace branch must NOT emit Set-Cookie (no rotation oracle)"
    )

    grace = await _list_audit(session_factory, "auth.session.grace_accept")
    # Exactly one event from the second /refresh (the first /refresh was
    # a normal rotation and emits auth.session.rotated, not grace).
    assert len(grace) == 1, f"expected 1 grace_accept event, got {len(grace)}"
    assert grace[0].detail["via_already_rotated"] is False
    assert grace[0].detail["old_jti"] == old_jti
    assert grace[0].detail["sid"] == sid
