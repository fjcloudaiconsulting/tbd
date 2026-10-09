"""PR 3 — Rotation grace window + rotation + verify fallback tests.

Pins every architect-emphasized risk in
``specs/2026-05-17-backend-session-model.md`` §8 PR 3:

1. Cross-tab race produces exactly one rotation + one grace acceptance
   (the canonical no-double-issue pin — the head check under the family
   row lock is what makes this pass).
2. Replay of an already-rotated jti AFTER the 30s grace window fails.
3. ``jti_collision`` path: under a forced-collision RNG the first
   rotate call returns ``jti_collision``, the router regenerates, the second
   call succeeds; under always-collide RNG the router returns 503
   with the ``auth.session.rotated.failed`` audit row.
4. Grace branch family check: if logout deletes the family
   inside the grace window, the grace branch rejects (architect
   P1.1 — closes the logout-vs-rotation race).
5. ``/verify`` mirrors ``/refresh`` — accepts a grace ticket when the
   family is still alive, rejects when the family is gone.
6. Concurrent rotation produces the correct audit shape: one
   ``auth.session.rotated`` AND one
   ``auth.session.grace_accept {via_already_rotated: true}``.
7. ``sid`` mismatch on the grace branch rejects.
8. Replay-after-logout class — within the 30s grace window, if logout
   has deleted the family, the grace branch must reject.

Concurrency tests use ``asyncio.gather`` + ``asyncio.Event`` gating,
NEVER ``asyncio.sleep`` — the architect's #1 named concern for flake.
"""
from __future__ import annotations

import asyncio
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

from app import state_db
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
from app.security import create_refresh_token, decode_refresh_jti_sid, hash_password

from tests.conftest import expire_grace, set_refresh_cookie


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


@pytest.fixture
def rotate_barrier(monkeypatch):
    """Hold every ``session_rotate`` call until two have arrived, so both
    racing ``/refresh`` requests have validated the old head before either
    rotates. The loser is then held until the winner's audit write is done:
    the in-memory test DB is a single shared connection, so two concurrent
    audit sessions would collide (a harness artifact, not production
    behavior). Event gating only, never ``asyncio.sleep``."""
    from app.routers import auth as auth_module

    real = state_db.session_rotate
    real_record = auth_module._record_session_rotated
    state = {"arrived": 0, "release": asyncio.Event(), "winner_done": asyncio.Event()}

    async def _gated(*args, **kwargs):
        state["arrived"] += 1
        if state["arrived"] >= 2:
            state["release"].set()
        await asyncio.wait_for(state["release"].wait(), timeout=5)
        result = await real(*args, **kwargs)
        if result == state_db.SESSION_ROTATE_ALREADY_ROTATED:
            await asyncio.wait_for(state["winner_done"].wait(), timeout=5)
        return result

    async def _record(*args, **kwargs):
        try:
            return await real_record(*args, **kwargs)
        finally:
            state["winner_done"].set()

    monkeypatch.setattr(state_db, "session_rotate", _gated)
    monkeypatch.setattr(auth_module, "_record_session_rotated", _record)


# ── 1. Replay AFTER the grace window (manual expiry) ─────────────────────────


async def test_replay_after_grace_window_returns_401(session_factory):
    """Grace window is 30s. Once the old jti is neither the head nor
    graced, it must 401.

    We can't wait 31s in unit tests; instead we age the members past the
    window (equivalent to the grace expiring) after the rotation.
    """
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        old_jti, sid = decode_refresh_jti_sid(token)

        # First refresh rotates.
        set_refresh_cookie(client, token)
        first = client.post("/api/v1/auth/refresh")
        assert first.status_code == 200
        # old_jti is graced now.
        assert state_db._grace(old_jti) is not None

        # Simulate the window expiring.
        expire_grace(sid)

        # Replay the old cookie.
        set_refresh_cookie(client, token)
        second = client.post("/api/v1/auth/refresh")
    assert second.status_code == 401
    assert second.json()["detail"] == "Session has been invalidated"


# ── 2. Cross-tab race — gated concurrent /refresh produces 1 + 1 ─────────────


async def test_concurrent_refresh_one_winner_one_grace(session_factory, rotate_barrier):
    """Two concurrent ``/refresh`` calls with the same pre-rotation cookie
    produce exactly: one rotation winner and one grace-path loser,
    both 200, both emitting a Set-Cookie whose JWT decodes to the SAME
    successor jti (2026-05-19 catch-up fix). Zero 401s. The loser's
    Set-Cookie comes from ``_issue_catchup_refresh_cookie`` reading
    ``grace_row["successor_jti"]`` — NOT from the loser's locally-minted
    candidate jti, which was never stored.

    Implementation: gate BOTH coroutines at ``session_rotate`` with an
    ``asyncio.Event``, release them simultaneously. The family row lock
    makes the second call observe the winner's write (new head) and
    return ``already_rotated``. The router then enters the
    already_rotated re-probe path and issues the catch-up cookie.

    Without the head check under the lock, both calls would rotate — the test
    would observe two DISTINCT successor jtis instead of two identical
    ones. This is the canonical no-double-issue pin AND the
    catch-up-cookie-convergence pin in one test.
    """
    await _seed_user(session_factory)
    app = _make_app(session_factory)

    # Boot one TestClient to log in (sync), then drive concurrency with
    # httpx.AsyncClient.
    with TestClient(app) as client:
        token = _login(client)
    old_jti, sid = decode_refresh_jti_sid(token)

    async with _httpx_app_client(app) as ac:
        set_refresh_cookie(ac, token)

        async def _do_refresh():
            return await ac.post("/api/v1/auth/refresh")

        task_a = asyncio.create_task(_do_refresh())
        task_b = asyncio.create_task(_do_refresh())
        res_a, res_b = await asyncio.gather(task_a, task_b)

    statuses = sorted([res_a.status_code, res_b.status_code])
    assert statuses == [200, 200], (
        f"expected two 200s, got {statuses}: A={res_a.text!r} B={res_b.text!r}"
    )

    cookies_a = _canonical_refresh_cookie(res_a.headers)
    cookies_b = _canonical_refresh_cookie(res_b.headers)
    set_cookies = [c for c in (cookies_a, cookies_b) if c is not None]
    # 2026-05-19 catch-up fix: both responses now emit Set-Cookie. The
    # winner sets it from the normal rotation path; the loser sets it
    # via ``_issue_catchup_refresh_cookie`` after the
    # ``already_rotated`` re-probe. Both cookies MUST decode to the
    # same successor jti so the browser converges on whichever
    # response lands last.
    assert len(set_cookies) == 2, (
        f"expected exactly two Set-Cookies (winner + loser catch-up), "
        f"got {len(set_cookies)}: A={cookies_a!r} B={cookies_b!r}"
    )

    decoded_jtis = []
    for raw in set_cookies:
        token = _refresh_token_from_set_cookie(raw)
        jti, sid_decoded = decode_refresh_jti_sid(token)
        assert sid_decoded == sid
        decoded_jtis.append(jti)
    assert len(set(decoded_jtis)) == 1, (
        f"winner + loser Set-Cookies must decode to the SAME successor "
        f"jti; got {decoded_jtis}. Browser convergence depends on this."
    )
    winner_jti = decoded_jtis[0]
    assert winner_jti != old_jti
    assert state_db._validate(winner_jti) is not None
    # old_jti is graced.
    assert state_db._grace(old_jti) is not None
    # old_jti is no longer the head.
    assert state_db._validate(old_jti) is None


# ── 3. Audit shape on the cross-tab race ─────────────────────────────────────


async def test_concurrent_refresh_emits_one_rotated_and_one_grace_accept(
    session_factory, rotate_barrier
):
    """The race in the previous test must emit BOTH audit events:
    one ``auth.session.rotated`` (winner) AND one
    ``auth.session.grace_accept {via_already_rotated: true}`` (loser)."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)

    with TestClient(app) as client:
        token = _login(client)
    old_jti, sid = decode_refresh_jti_sid(token)

    async with _httpx_app_client(app) as ac:
        set_refresh_cookie(ac, token)

        async def _do_refresh():
            return await ac.post("/api/v1/auth/refresh")

        task_a = asyncio.create_task(_do_refresh())
        task_b = asyncio.create_task(_do_refresh())
        await asyncio.gather(task_a, task_b)

    rotated = await _list_audit(session_factory, "auth.session.rotated")
    grace = await _list_audit(session_factory, "auth.session.grace_accept")
    assert len(rotated) == 1, f"expected 1 rotated event, got {len(rotated)}"
    assert len(grace) == 1, f"expected 1 grace_accept event, got {len(grace)}"
    assert grace[0].detail["via_already_rotated"] is True
    assert grace[0].detail["sid"] == sid
    assert grace[0].detail["old_jti"] == old_jti


# ── 4. /verify accepts a grace ticket (family alive) ────────────────────────


async def test_verify_accepts_grace_ticket_when_family_alive(
    session_factory
):
    """``/verify`` mirrors ``/refresh`` grace fallback (spec §5.2)."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        old_jti, sid = decode_refresh_jti_sid(token)

        # Rotate once so old_jti is graced.
        set_refresh_cookie(client, token)
        r1 = client.post("/api/v1/auth/refresh")
        assert r1.status_code == 200
        assert state_db._grace(old_jti) is not None

        # /verify with the OLD cookie — not the head, graced, family alive.
        set_refresh_cookie(client, token)
        res = client.post("/api/v1/auth/verify")
    assert res.status_code == 200, res.text
    # Invariant: no Set-Cookie from /verify.
    assert _canonical_refresh_cookie(res.headers) is None


# ── 5. /verify rejects grace ticket when family deleted ─────────────────────


async def test_verify_rejects_grace_ticket_when_family_deleted(
    session_factory
):
    """Inside the grace window BUT the family has been deleted (concurrent
    logout) — ``/verify`` must reject. Without the family check
    ``/verify`` would accept while ``/refresh`` would reject — exactly
    the inconsistency the architect called out."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        old_jti, sid = decode_refresh_jti_sid(token)

        set_refresh_cookie(client, token)
        r1 = client.post("/api/v1/auth/refresh")
        assert r1.status_code == 200

        # Simulate concurrent logout: revoke the family.
        state_db._revoke_family(sid)

        set_refresh_cookie(client, token)
        res = client.post("/api/v1/auth/verify")
    assert res.status_code == 401


# ── 6. /refresh grace branch rejects when family deleted ────────────────────


async def test_refresh_grace_branch_rejects_when_family_deleted(
    session_factory
):
    """Architect P1.1 — even within the 30s grace window, if logout has
    deleted the family the grace branch must reject. Replay-after-
    logout class."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        old_jti, sid = decode_refresh_jti_sid(token)

        # Rotate so old_jti is only graced.
        set_refresh_cookie(client, token)
        r1 = client.post("/api/v1/auth/refresh")
        assert r1.status_code == 200
        assert state_db._grace(old_jti) is not None

        # Concurrent logout: revoke the family.
        state_db._revoke_family(sid)

        set_refresh_cookie(client, token)
        res = client.post("/api/v1/auth/refresh")
    assert res.status_code == 401, res.text
    assert res.json()["detail"] == "Session has been invalidated"


# ── 7. /refresh grace branch rejects when sid in grace row differs ──────────


async def test_refresh_grace_branch_rejects_on_sid_mismatch(
    session_factory
):
    """Defence against an attacker minting a JWT with someone else's
    jti + their own sid. The graced jti's stored sid must match the
    JWT's sid claim."""
    seeded = await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        old_jti, sid = decode_refresh_jti_sid(token)

        set_refresh_cookie(client, token)
        r1 = client.post("/api/v1/auth/refresh")
        assert r1.status_code == 200
        # A JWT with the graced jti but another sid.
        token, _, _ = create_refresh_token(
            seeded["user_id"], sid="deadbeef-not-the-real-sid", jti=old_jti
        )

        set_refresh_cookie(client, token)
        res = client.post("/api/v1/auth/refresh")
    assert res.status_code == 401


# ── 8. jti_collision: forced single-collision RNG retries and succeeds ──────


async def test_jti_collision_retries_and_succeeds(
    session_factory, monkeypatch
):
    """Forced-collision RNG returns the SAME jti for two successive calls.
    First rotate call returns ``jti_collision`` (the member insert hits
    the primary key because that jti is already a member). Router regenerates and the
    second attempt succeeds. Audit ``auth.session.rotated`` emitted ONCE.

    We rig the collision by patching ``secrets.token_urlsafe`` inside
    ``app.security`` so the FIRST two refresh-token mints get the same
    jti, then the third (the router's retry) gets a fresh one.
    """
    import secrets as _secrets

    real_token_urlsafe = _secrets.token_urlsafe
    # First call to create_refresh_token returns the colliding jti
    # (first rotate attempt -> jti_collision). Second call gets a fresh
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

    # Seed a member that will collide with the first patched jti we hand
    # to the rotate call.
    state_db._issue("collide-with-existing-primary", "unrelated", 999999, 3600)

    monkeypatch.setattr(
        "app.security.secrets.token_urlsafe", _patched_token_urlsafe
    )

    with TestClient(app) as client:
        set_refresh_cookie(client, token)
        res = client.post("/api/v1/auth/refresh")
    assert res.status_code == 200, res.text
    assert _canonical_refresh_cookie(res.headers) is not None

    rotated = await _list_audit(session_factory, "auth.session.rotated")
    assert len(rotated) == 1, f"expected 1 rotated event, got {len(rotated)}"
    failed = await _list_audit(session_factory, "auth.session.rotated.failed")
    assert failed == []


# ── 9. jti_collision: always-collide RNG => 503 + audit ─────────────────────


async def test_jti_collision_double_failure_returns_503(
    session_factory, monkeypatch
):
    """If the RNG collides on BOTH attempts the router returns 503 and
    emits ``auth.session.rotated.failed`` exactly once."""

    def _always_collide(n: int = 16) -> str:
        return "collide-with-existing-primary"

    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)

    state_db._issue("collide-with-existing-primary", "unrelated", 999999, 3600)

    monkeypatch.setattr(
        "app.security.secrets.token_urlsafe", _always_collide
    )

    with TestClient(app) as client:
        set_refresh_cookie(client, token)
        res = client.post("/api/v1/auth/refresh")
    assert res.status_code == 503, res.text
    assert _canonical_refresh_cookie(res.headers) is None

    failed = await _list_audit(session_factory, "auth.session.rotated.failed")
    assert len(failed) == 1, f"expected 1 rotated.failed event, got {len(failed)}"
    rotated = await _list_audit(session_factory, "auth.session.rotated")
    assert rotated == []


# ── 10. Direct grace path (the typical cross-tab race after the fact) ───────


async def test_refresh_direct_grace_path_emits_catchup_cookie_and_audit(
    session_factory
):
    """The "boring" cross-tab race: tab A rotates first, tab B's
    ``/refresh`` arrives later still carrying the old cookie. The
    old jti is already rotated out and graced, the family
    is alive — the validator hands us ``redis_state == "grace"``.

    2026-05-19 catch-up fix: the router now emits a catch-up
    Set-Cookie pointing at the successor jti written by tab A's
    rotation, so tab B converges to the live primary instead of
    holding a stale cookie that locks out 30s later. Audit:
    ``auth.session.grace_accept {via_already_rotated: false}`` (the
    audit shape is unchanged — the catch-up emits a cookie but is
    still semantically a grace acceptance, not a rotation).
    """
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        old_jti, sid = decode_refresh_jti_sid(token)

        set_refresh_cookie(client, token)
        r1 = client.post("/api/v1/auth/refresh")
        assert r1.status_code == 200
        # Capture the winner's successor jti from r1's Set-Cookie.
        winner_raw = _canonical_refresh_cookie(r1.headers)
        assert winner_raw is not None
        winner_token = _refresh_token_from_set_cookie(winner_raw)
        winner_jti, _ = decode_refresh_jti_sid(winner_token)

        # At this point old_jti is rotated out, graced, family alive.
        # Tab B replays the old cookie.
        set_refresh_cookie(client, token)
        res = client.post("/api/v1/auth/refresh")

    assert res.status_code == 200, res.text
    catchup_raw = _canonical_refresh_cookie(res.headers)
    assert catchup_raw is not None, (
        "grace branch must now emit catch-up Set-Cookie (2026-05-19 fix)"
    )
    catchup_token = _refresh_token_from_set_cookie(catchup_raw)
    catchup_jti, catchup_sid = decode_refresh_jti_sid(catchup_token)
    assert catchup_sid == sid
    # Critical: the catch-up cookie points at the WINNER's successor
    # jti — the live head — not a freshly-minted random.
    assert catchup_jti == winner_jti, (
        f"catch-up cookie must point at winner's successor; "
        f"got {catchup_jti!r}, expected {winner_jti!r}"
    )

    grace = await _list_audit(session_factory, "auth.session.grace_accept")
    # Exactly one event from the second /refresh (the first /refresh was
    # a normal rotation and emits auth.session.rotated, not grace).
    assert len(grace) == 1, f"expected 1 grace_accept event, got {len(grace)}"
    assert grace[0].detail["via_already_rotated"] is False
    assert grace[0].detail["old_jti"] == old_jti
    assert grace[0].detail["sid"] == sid
