"""PR 4 — Per-session logout: family revoke (spec §5.3).

Pins every architect-emphasized risk for the logout-vs-rotation path:

1. Logout-after-rotation revokes the entire family — within the 30s
   grace window, a sibling tab holding the pre-rotation cookie cannot
   refresh successfully. The family-set delete in Round A makes any
   subsequent /refresh see ``SISMEMBER`` return 0 (rotation Lua) or
   ``EXISTS by_sid`` return 0 (grace branch).
2. Concurrent logout-vs-rotation produces Lua ``session_revoked`` —
   gated with ``asyncio.Event`` so the rotate enters the Lua body
   AFTER logout's Round A lands.
3. ``/verify`` rejects a grace ticket after logout (mirrors PR 3
   semantics on the logout side).
4. Multi-cookie logout (rare but real after PR #211 cookie-path
   migration): two refresh cookies for two distinct ``sid`` values
   revoke both families.
5. Anonymous logout — no cookie at all. 200, audit emitted with
   ``sid_count=0, jti_count=0``.
6. Cookie present but undecodable (corrupt JWT). 200, audit emitted,
   cookie cleared.
7. Logout does NOT write ``sessions_invalidated_at`` — the 2026-05-16
   false-logout incident regression.

Concurrency tests use ``asyncio.Event`` gating, NEVER
``asyncio.sleep`` — the architect's #1 named concern for flake.
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
from app.security import (
    create_access_token,
    decode_refresh_jti_sid,
    hash_password,
)


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


async def _seed_user(
    factory: async_sessionmaker[AsyncSession], *, username: str = "alice"
) -> dict:
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
            is_superadmin=False,
            is_active=True,
            email_verified=True,
        )
        db.add(user)
        await db.commit()
        return {"org_id": org.id, "user_id": user.id, "username": username}


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


def _delete_cookie_headers(headers) -> list[str]:
    """All Set-Cookie headers for ``refresh_token`` that look like a
    delete (``Max-Age=0``). PR 4 logout clears at BOTH ``Path=/`` and
    the legacy ``Path=/api/v1/auth/refresh``."""
    return [
        c for c in _set_cookie_values_for(headers, "refresh_token")
        if "Max-Age=0" in c
    ]


def _refresh_token_from_set_cookie(raw: str) -> str:
    head = raw.split(";", 1)[0].strip()
    name, _, value = head.partition("=")
    assert name == "refresh_token"
    return value


def _login(client: TestClient, *, username: str = "alice") -> str:
    res = client.post(
        "/api/v1/auth/login",
        json={"login": username, "password": PASSWORD},
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


# ─── 1. Logout-after-rotation revokes the entire family (architect P1.1) ────


async def test_logout_after_rotation_revokes_entire_family(
    session_factory, fake_redis
):
    """Login -> rotate (new_jti primary, old_jti grace). Logout with the
    new cookie. Old jti must NOT re-authenticate even though its grace
    key has not yet TTL-expired — the family-set delete in Round A is
    what closes this race (architect P1.1 + PR #301 follow-up).
    """
    seed = await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        old_jti, sid = decode_refresh_jti_sid(token)

        # Rotate so old_jti only has a grace key, new_jti is the primary.
        r1 = client.post("/api/v1/auth/refresh", cookies={"refresh_token": token})
        assert r1.status_code == 200
        new_raw = _canonical_refresh_cookie(r1.headers)
        assert new_raw is not None
        new_token = _refresh_token_from_set_cookie(new_raw)
        new_jti, new_sid = decode_refresh_jti_sid(new_token)
        assert new_sid == sid  # sid is stable across rotation

        # Sanity: both keys + family set are alive before logout.
        assert f"auth:session:{new_jti}" in fake_redis._kv
        assert f"auth:session:grace:{old_jti}" in fake_redis._kv
        assert new_jti in fake_redis._sets[f"auth:session:by_sid:{sid}"]
        assert old_jti in fake_redis._sets[f"auth:session:by_sid:{sid}"]

        # Logout using the CURRENT (rotated) cookie. Authorization header
        # carries a valid access token so audit binds to the actor.
        access = create_access_token(
            seed["user_id"],
            seed["org_id"],
            Role.OWNER.value,
        )
        logout = client.post(
            "/api/v1/auth/logout",
            cookies={"refresh_token": new_token},
            headers={"Authorization": f"Bearer {access}"},
        )
        assert logout.status_code == 200, logout.text

        # Family set is gone — Round A's atomic DEL.
        assert f"auth:session:by_sid:{sid}" not in fake_redis._sets

        # Primary + grace keys for BOTH jtis are gone too — Round B.
        assert f"auth:session:{new_jti}" not in fake_redis._kv
        assert f"auth:session:grace:{old_jti}" not in fake_redis._kv

        # Replay the PRE-rotation cookie. Grace key is gone AND family
        # is gone, so /refresh must 401.
        replay = client.post(
            "/api/v1/auth/refresh", cookies={"refresh_token": token}
        )
    assert replay.status_code == 401
    assert replay.json()["detail"] == "Session has been invalidated"


# ─── 2. Concurrent logout-vs-rotation produces Lua session_revoked ──────────


async def test_concurrent_logout_vs_rotation_returns_session_revoked(
    session_factory, fake_redis
):
    """Two coroutines: logout + rotate. Gate the rotate Lua entry with
    an ``asyncio.Event`` so it ONLY runs AFTER logout's Round A has
    deleted the family set.

    Expected: the rotate Lua returns ``session_revoked`` (its first
    guard finds ``SISMEMBER`` = 0), the router maps to 401, NO new
    primary key is written, NO new family member added.
    """
    seed = await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        old_jti, sid = decode_refresh_jti_sid(token)

    # Two events drive the gating:
    #   * ``logout_done`` — set by the logout coroutine AFTER its
    #     ``redis_client.session_revoke_family`` call returns.
    #   * Inside the fake's ``eval`` we monkey-patch a hook that waits
    #     for ``logout_done`` before the script body executes. That
    #     ensures the Lua script runs after the family is gone.
    logout_done = asyncio.Event()

    original_eval = fake_redis.eval

    async def gated_eval(script, numkeys, *args):
        # Wait until logout's Round A has landed before letting the
        # rotate Lua proceed. Pure event signaling — no sleeps.
        await logout_done.wait()
        return await original_eval(script, numkeys, *args)

    fake_redis.eval = gated_eval

    access = create_access_token(
        seed["user_id"], seed["org_id"], Role.OWNER.value
    )

    async with _httpx_app_client(app) as ac:
        async def _do_refresh():
            return await ac.post(
                "/api/v1/auth/refresh", cookies={"refresh_token": token}
            )

        async def _do_logout():
            res = await ac.post(
                "/api/v1/auth/logout",
                cookies={"refresh_token": token},
                headers={"Authorization": f"Bearer {access}"},
            )
            logout_done.set()
            return res

        rotate_task = asyncio.create_task(_do_refresh())
        logout_task = asyncio.create_task(_do_logout())
        rotate_res, logout_res = await asyncio.gather(rotate_task, logout_task)

    assert logout_res.status_code == 200
    assert rotate_res.status_code == 401, rotate_res.text
    assert rotate_res.json()["detail"] == "Session has been invalidated"

    # No successor primary was written.
    primary_keys = [
        k for k in fake_redis._kv if k.startswith("auth:session:")
        and not k.startswith("auth:session:grace:")
        and not k.startswith("auth:session:by_sid:")
    ]
    assert primary_keys == [], (
        f"expected no primary keys after revoked rotation, got {primary_keys}"
    )
    # Family set is gone.
    assert f"auth:session:by_sid:{sid}" not in fake_redis._sets


# ─── 3. /verify rejects a grace ticket after logout ─────────────────────────


async def test_verify_rejects_grace_ticket_after_logout(
    session_factory, fake_redis
):
    """Spec §5.2 mirror for the logout side. After logout deletes the
    family set, /verify must reject even within the 30s grace window."""
    seed = await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        old_jti, sid = decode_refresh_jti_sid(token)

        # Rotate so the grace key exists for old_jti.
        r1 = client.post("/api/v1/auth/refresh", cookies={"refresh_token": token})
        assert r1.status_code == 200
        new_token = _refresh_token_from_set_cookie(
            _canonical_refresh_cookie(r1.headers)
        )

        # Logout — family set deleted.
        access = create_access_token(
            seed["user_id"], seed["org_id"], Role.OWNER.value
        )
        logout = client.post(
            "/api/v1/auth/logout",
            cookies={"refresh_token": new_token},
            headers={"Authorization": f"Bearer {access}"},
        )
        assert logout.status_code == 200

        # /verify with the PRE-rotation cookie. Grace key was deleted
        # by Round B, but even if some grace-only edge case lingered,
        # the family-set check in the grace branch rejects.
        verify = client.post(
            "/api/v1/auth/verify", cookies={"refresh_token": token}
        )
    assert verify.status_code == 401


# ─── 4. Multi-cookie logout (two distinct sids) ─────────────────────────────


async def test_multi_cookie_logout_revokes_each_family(session_factory, fake_redis):
    """Browser carries two refresh cookies for two distinct ``sid``s
    (rare but real with the PR #211 cookie-path migration overlap).
    Logout must revoke BOTH families and the audit detail must reflect
    ``{sid_count: 2, jti_count: ...}``."""
    seed = await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token_a = _login(client)
        jti_a, sid_a = decode_refresh_jti_sid(token_a)

    # Force a SEPARATE second session (different sid) by logging in
    # again after clearing the cookie context. The previous family is
    # still alive in Redis.
    app2 = _make_app(session_factory)
    with TestClient(app2) as client2:
        token_b = _login(client2)
        jti_b, sid_b = decode_refresh_jti_sid(token_b)

    assert sid_a != sid_b
    assert f"auth:session:by_sid:{sid_a}" in fake_redis._sets
    assert f"auth:session:by_sid:{sid_b}" in fake_redis._sets

    # Hand-craft a cookie header with BOTH refresh_token values so the
    # extractor walks the raw header and decodes both.
    cookie_header = f"refresh_token={token_a}; refresh_token={token_b}"
    access = create_access_token(
        seed["user_id"], seed["org_id"], Role.OWNER.value
    )

    # Use a fresh TestClient — we need raw header control. Use a third
    # client + the ASGITransport to bypass cookie-jar collapsing.
    async with _httpx_app_client(app) as ac:
        res = await ac.post(
            "/api/v1/auth/logout",
            headers={
                "Authorization": f"Bearer {access}",
                "Cookie": cookie_header,
            },
        )

    assert res.status_code == 200, res.text
    assert f"auth:session:by_sid:{sid_a}" not in fake_redis._sets
    assert f"auth:session:by_sid:{sid_b}" not in fake_redis._sets

    audit = await _list_audit(session_factory, "auth.session.terminated")
    assert len(audit) == 1
    assert audit[0].detail["sid_count"] == 2
    assert audit[0].detail["jti_count"] == 2  # one jti per family


# ─── 5. Anonymous logout (no cookie) ────────────────────────────────────────


async def test_anonymous_logout_succeeds_with_zero_counts(session_factory, fake_redis):
    """No refresh cookie at all. Logout still returns 200, clears the
    cookie (no-op since none arrived), emits the audit row with
    ``sid_count=0, jti_count=0, outcome=success``."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post("/api/v1/auth/logout")
    assert res.status_code == 200
    assert res.json()["detail"] == "Logged out"

    # Delete-cookie headers still emitted so the browser drops anything
    # it may still have.
    deletes = _delete_cookie_headers(res.headers)
    assert any("Path=/" in d for d in deletes), (
        f"missing Path=/ delete-cookie among {deletes}"
    )

    audit = await _list_audit(session_factory, "auth.session.terminated")
    assert len(audit) == 1
    assert audit[0].detail["sid_count"] == 0
    assert audit[0].detail["jti_count"] == 0
    assert audit[0].outcome == "success"


# ─── 6. Cookie present but undecodable (corrupt JWT) ────────────────────────


async def test_corrupt_refresh_cookie_logout_still_clears(session_factory, fake_redis):
    """Cookie value is not a valid refresh JWT. Logout swallows the
    decode error, clears the cookie, emits a 200 + audit with
    ``sid_count=0`` (no sids could be extracted)."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/logout",
            cookies={"refresh_token": "this-is-not-a-jwt"},
        )
    assert res.status_code == 200
    deletes = _delete_cookie_headers(res.headers)
    assert any(f"Path={LEGACY_REFRESH_COOKIE_PATH}" in d for d in deletes), (
        f"missing legacy-path delete-cookie among {deletes}"
    )
    assert any(
        "Path=/" in d and f"Path={LEGACY_REFRESH_COOKIE_PATH}" not in d
        for d in deletes
    )

    audit = await _list_audit(session_factory, "auth.session.terminated")
    assert len(audit) == 1
    assert audit[0].detail["sid_count"] == 0
    assert audit[0].outcome == "success"


# ─── 7. Logout does NOT write sessions_invalidated_at ───────────────────────


async def test_logout_does_not_write_sessions_invalidated_at(
    session_factory, fake_redis
):
    """The 2026-05-16 false-logout incident regression pin. Capture the
    user's ``sessions_invalidated_at`` before logout, run logout, assert
    the field is unchanged (still its pre-logout value)."""
    seed = await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        access = create_access_token(
            seed["user_id"], seed["org_id"], Role.OWNER.value
        )

        async with session_factory() as db:
            row = await db.execute(
                select(User).where(User.id == seed["user_id"])
            )
            user_before = row.scalar_one()
            cutoff_before = user_before.sessions_invalidated_at

        res = client.post(
            "/api/v1/auth/logout",
            cookies={"refresh_token": token},
            headers={"Authorization": f"Bearer {access}"},
        )
    assert res.status_code == 200

    async with session_factory() as db:
        row = await db.execute(
            select(User).where(User.id == seed["user_id"])
        )
        user_after = row.scalar_one()
        cutoff_after = user_after.sessions_invalidated_at

    assert cutoff_after == cutoff_before, (
        "POST /auth/logout MUST NOT touch sessions_invalidated_at — that "
        "is the global-cutoff mechanism reserved for spec §6 triggers. "
        f"before={cutoff_before!r}, after={cutoff_after!r}"
    )


# ─── 8. Audit row binds to the calling user when bearer is present ─────────


async def test_logout_audit_binds_to_actor_when_bearer_present(
    session_factory, fake_redis
):
    """The audit row records ``actor_user_id`` + ``actor_email`` derived
    from the Authorization bearer when present. Important so the
    /admin/audit feed can attribute the logout to the right user."""
    seed = await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        access = create_access_token(
            seed["user_id"], seed["org_id"], Role.OWNER.value
        )
        res = client.post(
            "/api/v1/auth/logout",
            cookies={"refresh_token": token},
            headers={"Authorization": f"Bearer {access}"},
        )
    assert res.status_code == 200

    audit = await _list_audit(session_factory, "auth.session.terminated")
    assert len(audit) == 1
    assert audit[0].actor_user_id == seed["user_id"]
    assert audit[0].actor_email == f"{seed['username']}@example.com"
    assert audit[0].detail["sid_count"] == 1
    assert audit[0].detail["jti_count"] == 1


# ─── 9. Logout clears BOTH the canonical and legacy cookie paths ────────────


async def test_logout_clears_canonical_and_legacy_cookie_paths(
    session_factory, fake_redis
):
    """PR #211 cookie-shadow trap: even after logout, the browser may
    still carry a legacy ``Path=/api/v1/auth/refresh`` cookie. Logout
    must emit delete-cookie headers for BOTH paths."""
    seed = await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        token = _login(client)
        access = create_access_token(
            seed["user_id"], seed["org_id"], Role.OWNER.value
        )
        res = client.post(
            "/api/v1/auth/logout",
            cookies={"refresh_token": token},
            headers={"Authorization": f"Bearer {access}"},
        )
    assert res.status_code == 200
    deletes = _delete_cookie_headers(res.headers)
    has_root = any(
        "Path=/" in d and f"Path={LEGACY_REFRESH_COOKIE_PATH}" not in d
        for d in deletes
    )
    has_legacy = any(f"Path={LEGACY_REFRESH_COOKIE_PATH}" in d for d in deletes)
    assert has_root, f"missing Path=/ delete-cookie among {deletes}"
    assert has_legacy, f"missing legacy-path delete-cookie among {deletes}"


# ─── 10. Other devices stay authenticated (AC2 pin) ─────────────────────────


async def test_logout_one_device_leaves_other_device_authenticated(
    session_factory, fake_redis
):
    """AC2 from the spec: a second device (separate session, separate
    sid) must remain authenticated after the first device logs out."""
    seed = await _seed_user(session_factory)

    # Device A logs in.
    app_a = _make_app(session_factory)
    with TestClient(app_a) as client_a:
        token_a = _login(client_a)
        jti_a, sid_a = decode_refresh_jti_sid(token_a)

    # Device B logs in — same user, separate cookie jar.
    app_b = _make_app(session_factory)
    with TestClient(app_b) as client_b:
        token_b = _login(client_b)
        jti_b, sid_b = decode_refresh_jti_sid(token_b)

    assert sid_a != sid_b

    # Device A logs out.
    access_a = create_access_token(
        seed["user_id"], seed["org_id"], Role.OWNER.value
    )
    with TestClient(app_a) as client_a:
        logout = client_a.post(
            "/api/v1/auth/logout",
            cookies={"refresh_token": token_a},
            headers={"Authorization": f"Bearer {access_a}"},
        )
    assert logout.status_code == 200

    # Device A's family is gone, Device B's is untouched.
    assert f"auth:session:by_sid:{sid_a}" not in fake_redis._sets
    assert f"auth:session:by_sid:{sid_b}" in fake_redis._sets
    assert jti_b in fake_redis._sets[f"auth:session:by_sid:{sid_b}"]

    # Device B can still rotate.
    with TestClient(app_b) as client_b:
        rotate = client_b.post(
            "/api/v1/auth/refresh", cookies={"refresh_token": token_b}
        )
    assert rotate.status_code == 200
    new_raw = _canonical_refresh_cookie(rotate.headers)
    assert new_raw is not None
