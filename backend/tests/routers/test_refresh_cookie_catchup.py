"""Catch-up Set-Cookie on the /refresh grace branches — 2026-05-19.

Production trace at 2026-05-19T10:33–10:50 showed
``redis_primary_and_grace_missing`` firing repeatedly for the same sid,
with the browser sending old jtis that had been rotated past in Redis.
The two grace branches in ``/refresh`` returned ``TokenResponse``
without emitting ``Set-Cookie``, so when a cross-tab rotation race
made the browser end up on the grace branch, the cookie was never
advanced to the live successor primary. After the 30s grace TTL
expired, the next /refresh hit ``redis_primary_and_grace_missing``
and forced a logout.

The fix: both grace branches now mint a refresh JWT for the
``successor_jti`` recorded in the grace row and emit Set-Cookie
without writing Redis. Browser catches up to the live primary; the
30s lockout class is eliminated.

These tests pin the seven contracts required by the architect:
  1. Direct grace branch emits Set-Cookie for successor_jti.
  2. already_rotated re-probe branch emits Set-Cookie for successor_jti.
  3. Concurrent race: both responses carry cookies, both decode to the
     same successor jti and sid.
  4. The catch-up-issued cookie validates against the primary on the
     follow-up /refresh.
  5. No Redis write happens during catch-up issuance.
  6. Missing/dead successor_jti logs ``catchup_successor_unavailable``
     and fails closed with no cookie emitted.
  7. /verify still NEVER emits Set-Cookie, even on the grace path.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import jwt as pyjwt
import pytest
import pytest_asyncio
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_session_factory
from app.models import Base
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers.auth import router as auth_router
from app.security import create_refresh_token, hash_password


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


async def _seed_user(factory) -> dict[str, Any]:
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


def _seed_session_state(
    user_id: int,
    *,
    old_jti: str,
    successor_jti: str,
    sid: str,
    ttl_seconds: int = 30 * 86400,
) -> None:
    """Seed the autouse fake Redis so the validator sees:
    - primary key for ``old_jti`` MISSING (rotated past),
    - grace key for ``old_jti`` ALIVE with ``successor_jti`` payload,
    - primary key for ``successor_jti`` ALIVE,
    - family set for ``sid`` alive and contains both jtis.
    """
    from app import redis_client as rc

    client = rc.get_client()
    assert client is not None, "autouse fake-redis fixture missing"

    # Primary for successor jti (the winner's row).
    primary_payload = json.dumps(
        {"user_id": user_id, "sid": sid}, separators=(",", ":")
    )
    client._kv[f"auth:session:{successor_jti}"] = primary_payload
    # Old jti has NO primary (rotation deleted it) — leave unset.
    # Grace key for the old jti, points at the winning successor jti.
    grace_payload = json.dumps(
        {"user_id": user_id, "sid": sid, "successor_jti": successor_jti},
        separators=(",", ":"),
    )
    client._kv[f"auth:session:grace:{old_jti}"] = grace_payload
    # Family set: both jtis are members (the Lua winner adds the
    # successor, the loser's jti was added when it was originally
    # issued).
    client._sets[f"auth:session:by_sid:{sid}"].add(old_jti)
    client._sets[f"auth:session:by_sid:{sid}"].add(successor_jti)


def _mint_token_for(
    user_id: int, *, jti: str, sid: str, ttl_seconds: int = 30 * 86400
) -> str:
    """Build a refresh JWT with a specific jti/sid (so the validation
    chain sees the JWT's claims match the seeded Redis state)."""
    token, _, _ = create_refresh_token(
        user_id,
        ttl_seconds=ttl_seconds,
        sid=sid,
        jti=jti,
    )
    return token


# ── 1 + 2. Catch-up Set-Cookie on both grace branches ───────────────────


class TestGraceBranchCatchupCookie:
    @pytest.mark.asyncio
    async def test_direct_grace_branch_emits_setcookie_for_successor(
        self, session_factory
    ) -> None:
        """When the validator hits ``redis_state == "grace"``,
        /refresh now issues Set-Cookie with a JWT whose ``jti`` claim
        is the successor jti from the grace row."""
        seed = await _seed_user(session_factory)
        old_jti = "OLD-jti-c5952d8f"
        successor_jti = "NEW-jti-aa9b0787"
        sid = "test-sid-943e8eeb"
        _seed_session_state(
            seed["user_id"],
            old_jti=old_jti,
            successor_jti=successor_jti,
            sid=sid,
        )
        token = _mint_token_for(seed["user_id"], jti=old_jti, sid=sid)

        app = _make_app(session_factory)
        with TestClient(app) as client:
            res = client.post(
                "/api/v1/auth/refresh",
                cookies={"refresh_token": token},
            )

        assert res.status_code == 200, res.json()
        # Set-Cookie present.
        set_cookie_headers = [
            v for k, v in res.headers.items()
            if k.lower() == "set-cookie" and "refresh_token=" in v
        ]
        # Multiple cookies may be set (one canonical Path=/, one
        # legacy-clearing delete) — find the one carrying the JWT.
        new_cookie = next(
            (h for h in set_cookie_headers if "refresh_token=ey" in h),
            None,
        )
        assert new_cookie is not None, (
            f"Expected refresh_token Set-Cookie; got {set_cookie_headers}"
        )
        # Extract the JWT and decode — its ``jti`` claim MUST be the
        # successor jti, not the old one and not a freshly-minted random.
        cookie_value = new_cookie.split("refresh_token=", 1)[1].split(";", 1)[0]
        decoded = pyjwt.decode(
            cookie_value,
            app_settings.jwt_secret_key,
            algorithms=[app_settings.jwt_algorithm],
        )
        assert decoded["jti"] == successor_jti
        assert decoded["sid"] == sid
        assert decoded["sub"] == str(seed["user_id"])

    @pytest.mark.asyncio
    async def test_already_rotated_branch_emits_setcookie_for_successor(
        self, session_factory, monkeypatch
    ) -> None:
        """When the Lua rotation returns ``already_rotated`` and the
        grace re-probe succeeds, /refresh now issues Set-Cookie with
        a JWT for the grace row's successor jti."""
        from app.routers import auth as auth_module
        from app.redis_client import SESSION_ROTATE_ALREADY_ROTATED

        seed = await _seed_user(session_factory)
        # In this case the validator hits PRIMARY (not grace), so the
        # primary key for the cookie's jti must be alive. Then the Lua
        # rotation returns already_rotated; the handler re-probes the
        # grace key, which carries the successor written by the winner.
        winner_old_jti = "winner-old-jti"
        winner_successor = "winner-successor-aa9b"
        sid = "shared-sid"
        from app import redis_client as rc
        client = rc.get_client()
        client._kv[f"auth:session:{winner_old_jti}"] = json.dumps(
            {"user_id": seed["user_id"], "sid": sid},
            separators=(",", ":"),
        )
        client._sets[f"auth:session:by_sid:{sid}"].add(winner_old_jti)
        # Stub the Lua rotation to return already_rotated (the loser's
        # perspective) and pre-seed the grace key for ``winner_old_jti``
        # that the winner would have written.
        client._kv[f"auth:session:grace:{winner_old_jti}"] = json.dumps(
            {
                "user_id": seed["user_id"],
                "sid": sid,
                "successor_jti": winner_successor,
            },
            separators=(",", ":"),
        )
        # Successor primary alive too.
        client._kv[f"auth:session:{winner_successor}"] = json.dumps(
            {"user_id": seed["user_id"], "sid": sid},
            separators=(",", ":"),
        )
        client._sets[f"auth:session:by_sid:{sid}"].add(winner_successor)

        async def _stub_rotate(user_id, old_jti, sid_, **kwargs):
            # Return signature: (new_token, new_jti, sid, lua_result)
            return ("loser-token", "loser-new-jti", sid_, SESSION_ROTATE_ALREADY_ROTATED)

        monkeypatch.setattr(
            auth_module, "_rotate_refresh_session", _stub_rotate
        )

        token = _mint_token_for(
            seed["user_id"], jti=winner_old_jti, sid=sid
        )
        app = _make_app(session_factory)
        with TestClient(app) as cli:
            res = cli.post(
                "/api/v1/auth/refresh",
                cookies={"refresh_token": token},
            )

        assert res.status_code == 200, res.json()
        # The Set-Cookie's JWT must decode to ``winner_successor``,
        # NOT the loser's ``loser-new-jti``. This is the architect's
        # explicit guard against issuing a cookie for the loser's
        # phantom jti (which has no Redis row).
        set_cookie_headers = [
            v for k, v in res.headers.items()
            if k.lower() == "set-cookie" and "refresh_token=ey" in v
        ]
        assert len(set_cookie_headers) == 1, set_cookie_headers
        cookie_value = (
            set_cookie_headers[0]
            .split("refresh_token=", 1)[1]
            .split(";", 1)[0]
        )
        decoded = pyjwt.decode(
            cookie_value,
            app_settings.jwt_secret_key,
            algorithms=[app_settings.jwt_algorithm],
        )
        assert decoded["jti"] == winner_successor, (
            f"Catch-up cookie pointed at {decoded['jti']!r}, not "
            f"winner_successor={winner_successor!r}. Bug: the loser's "
            "newly-minted jti was used instead of grace_row.successor_jti."
        )


# ── 3. Concurrent race — both responses carry SAME successor jti ────────


class TestConcurrentRaceConvergence:
    @pytest.mark.asyncio
    async def test_two_grace_requests_emit_cookies_for_same_successor(
        self, session_factory
    ) -> None:
        """Two concurrent /refresh calls both hit the grace branch
        (rotation winner is some third request). Both MUST issue
        Set-Cookie with JWTs decoding to the SAME successor jti and
        sid — proves the catch-up logic is deterministic w.r.t. the
        grace row's successor field, not a per-request fresh mint."""
        seed = await _seed_user(session_factory)
        old_jti = "race-old-jti"
        successor_jti = "race-successor"
        sid = "race-sid"
        _seed_session_state(
            seed["user_id"],
            old_jti=old_jti,
            successor_jti=successor_jti,
            sid=sid,
        )
        token = _mint_token_for(seed["user_id"], jti=old_jti, sid=sid)
        app = _make_app(session_factory)
        decoded_jtis: list[str] = []
        with TestClient(app) as client:
            for _ in range(2):
                res = client.post(
                    "/api/v1/auth/refresh",
                    cookies={"refresh_token": token},
                )
                assert res.status_code == 200
                set_cookie = next(
                    v for k, v in res.headers.items()
                    if k.lower() == "set-cookie" and "refresh_token=ey" in v
                )
                value = (
                    set_cookie.split("refresh_token=", 1)[1].split(";", 1)[0]
                )
                payload = pyjwt.decode(
                    value,
                    app_settings.jwt_secret_key,
                    algorithms=[app_settings.jwt_algorithm],
                )
                decoded_jtis.append(payload["jti"])
                assert payload["sid"] == sid
        assert len(set(decoded_jtis)) == 1, (
            f"Both concurrent grace-path responses must carry the "
            f"SAME successor jti; got {decoded_jtis}"
        )
        assert decoded_jtis[0] == successor_jti


# ── 4. Follow-up /refresh using the catch-up cookie hits primary ────────


class TestCatchupCookieValidatesAgainstPrimary:
    @pytest.mark.asyncio
    async def test_followup_refresh_with_catchup_cookie_hits_primary(
        self, session_factory
    ) -> None:
        """After the grace-path response sets the cookie to the
        successor jti, the NEXT /refresh sent with that cookie must
        hit the primary path (NOT the grace path again) and emit a
        normal rotation. This proves the catch-up actually unsticks
        the browser."""
        seed = await _seed_user(session_factory)
        old_jti = "stuck-old-jti"
        successor_jti = "stuck-successor"
        sid = "stuck-sid"
        _seed_session_state(
            seed["user_id"],
            old_jti=old_jti,
            successor_jti=successor_jti,
            sid=sid,
        )
        token = _mint_token_for(seed["user_id"], jti=old_jti, sid=sid)

        app = _make_app(session_factory)
        with TestClient(app) as client:
            # Round 1: grace path → catch-up cookie.
            res1 = client.post(
                "/api/v1/auth/refresh",
                cookies={"refresh_token": token},
            )
            assert res1.status_code == 200
            set_cookie = next(
                v for k, v in res1.headers.items()
                if k.lower() == "set-cookie" and "refresh_token=ey" in v
            )
            new_token = (
                set_cookie.split("refresh_token=", 1)[1].split(";", 1)[0]
            )

            # Round 2: send the catch-up token. The validator should
            # find ``successor_jti`` as primary, run the rotation,
            # and emit a NEW Set-Cookie with a DIFFERENT jti.
            res2 = client.post(
                "/api/v1/auth/refresh",
                cookies={"refresh_token": new_token},
            )
            assert res2.status_code == 200
            second_set_cookie = next(
                v for k, v in res2.headers.items()
                if k.lower() == "set-cookie" and "refresh_token=ey" in v
            )
            second_value = (
                second_set_cookie
                .split("refresh_token=", 1)[1]
                .split(";", 1)[0]
            )
            second_decoded = pyjwt.decode(
                second_value,
                app_settings.jwt_secret_key,
                algorithms=[app_settings.jwt_algorithm],
            )
            # Different jti = real rotation happened (primary path),
            # not another catch-up (grace path would echo successor_jti).
            assert second_decoded["jti"] != successor_jti


# ── 5. No Redis write during catch-up issuance ──────────────────────────


class TestNoRedisWriteOnCatchup:
    @pytest.mark.asyncio
    async def test_catchup_does_not_call_session_issue_or_rotate(
        self, session_factory, monkeypatch
    ) -> None:
        """The catch-up helper must NOT call any Redis-writing helper.
        The winning rotation already wrote the successor primary; a
        second write here would either be redundant (best case) or
        clobber the row's TTL/data (worst case)."""
        seed = await _seed_user(session_factory)
        old_jti = "nowrite-old-jti"
        successor_jti = "nowrite-successor"
        sid = "nowrite-sid"
        _seed_session_state(
            seed["user_id"],
            old_jti=old_jti,
            successor_jti=successor_jti,
            sid=sid,
        )
        token = _mint_token_for(seed["user_id"], jti=old_jti, sid=sid)

        from app import redis_client as rc

        # Spy on the Redis-writing helpers. The grace path is read-only
        # so neither must be called.
        write_calls: list[str] = []

        async def _spy_session_issue(*args, **kwargs):
            write_calls.append("session_issue")
            return None

        async def _spy_session_rotate_lua(*args, **kwargs):
            write_calls.append("session_rotate_lua")
            return "ok"

        async def _spy_session_revoke_family(*args, **kwargs):
            write_calls.append("session_revoke_family")
            return []

        monkeypatch.setattr(rc, "session_issue", _spy_session_issue)
        monkeypatch.setattr(rc, "session_rotate_lua", _spy_session_rotate_lua)
        monkeypatch.setattr(
            rc, "session_revoke_family", _spy_session_revoke_family
        )

        app = _make_app(session_factory)
        with TestClient(app) as client:
            res = client.post(
                "/api/v1/auth/refresh",
                cookies={"refresh_token": token},
            )
        assert res.status_code == 200
        assert write_calls == [], (
            f"Catch-up path must not write Redis; calls observed: "
            f"{write_calls}"
        )


# ── 6. Missing / dead successor_jti fails closed ────────────────────────


class TestMissingSuccessorFailsClosed:
    @pytest.mark.asyncio
    async def test_missing_successor_jti_logs_and_401s(
        self, session_factory
    ) -> None:
        """Grace row exists but doesn't carry ``successor_jti`` (data
        corruption / future migration / handcrafted row). Helper must
        log ``catchup_successor_unavailable`` and 401 — never emit
        Set-Cookie for an unbound jti."""
        seed = await _seed_user(session_factory)
        old_jti = "no-successor-old"
        sid = "no-successor-sid"
        from app import redis_client as rc

        client = rc.get_client()
        # Grace key WITHOUT successor_jti.
        client._kv[f"auth:session:grace:{old_jti}"] = json.dumps(
            {"user_id": seed["user_id"], "sid": sid},
            separators=(",", ":"),
        )
        client._sets[f"auth:session:by_sid:{sid}"].add(old_jti)
        token = _mint_token_for(seed["user_id"], jti=old_jti, sid=sid)

        app = _make_app(session_factory)
        with structlog.testing.capture_logs() as captured:
            with TestClient(app) as cli:
                res = cli.post(
                    "/api/v1/auth/refresh",
                    cookies={"refresh_token": token},
                )
        assert res.status_code == 401
        # Reason log emitted.
        rejection_logs = [
            ev for ev in captured
            if ev.get("event") == "auth.refresh.rejected"
            and ev.get("reason") == "catchup_successor_unavailable"
        ]
        assert len(rejection_logs) == 1, (
            f"Expected one catchup_successor_unavailable; got: {captured}"
        )
        # No refresh_token Set-Cookie with a JWT value (the
        # legacy-cookie delete header is allowed).
        jwt_cookies = [
            v for k, v in res.headers.items()
            if k.lower() == "set-cookie" and "refresh_token=ey" in v
        ]
        assert jwt_cookies == [], (
            f"No JWT cookie may be emitted on fail-closed; got {jwt_cookies}"
        )

    @pytest.mark.asyncio
    async def test_successor_not_in_family_set_logs_and_401s(
        self, session_factory
    ) -> None:
        """P2 architect addition: PR #308 made family-set membership the
        authoritative revocation contract. Successor primary row alive
        + (user_id, sid) match but jti NOT in ``auth:session:by_sid:{sid}``
        (corrupted/partial Redis state) must fail closed — otherwise we
        emit a cookie the very next /refresh would 401 with
        ``family_member_missing``."""
        seed = await _seed_user(session_factory)
        old_jti = "orphan-old"
        successor_jti = "orphan-successor"
        sid = "orphan-sid"
        from app import redis_client as rc

        client = rc.get_client()
        # Grace row points at successor_jti.
        client._kv[f"auth:session:grace:{old_jti}"] = json.dumps(
            {
                "user_id": seed["user_id"],
                "sid": sid,
                "successor_jti": successor_jti,
            },
            separators=(",", ":"),
        )
        # Successor primary alive and bound to (user_id, sid)…
        client._kv[f"auth:session:{successor_jti}"] = json.dumps(
            {"user_id": seed["user_id"], "sid": sid},
            separators=(",", ":"),
        )
        # …BUT successor_jti is NOT in the family set. Family set
        # contains only ``old_jti`` (simulating a revocation that
        # dropped the successor while leaving the primary row alive,
        # or a Redis replica desync).
        client._sets[f"auth:session:by_sid:{sid}"].add(old_jti)
        token = _mint_token_for(seed["user_id"], jti=old_jti, sid=sid)

        app = _make_app(session_factory)
        with structlog.testing.capture_logs() as captured:
            with TestClient(app) as cli:
                res = cli.post(
                    "/api/v1/auth/refresh",
                    cookies={"refresh_token": token},
                )
        assert res.status_code == 401
        rejection_logs = [
            ev for ev in captured
            if ev.get("event") == "auth.refresh.rejected"
            and ev.get("reason") == "catchup_successor_unavailable"
            and ev.get("successor_family_member_missing") is True
        ]
        assert len(rejection_logs) == 1, (
            f"Expected catchup_successor_unavailable with "
            f"successor_family_member_missing=True; got: {captured}"
        )
        # No cookie emitted.
        jwt_cookies = [
            v for k, v in res.headers.items()
            if k.lower() == "set-cookie" and "refresh_token=ey" in v
        ]
        assert jwt_cookies == []

    @pytest.mark.asyncio
    async def test_successor_primary_missing_logs_and_401s(
        self, session_factory
    ) -> None:
        """Grace row carries ``successor_jti`` but the successor
        primary row is GONE (the successor was itself rotated past
        and its 30s grace also expired — pathological chain). Helper
        must fail closed."""
        seed = await _seed_user(session_factory)
        old_jti = "dead-successor-old"
        successor_jti = "dead-successor"
        sid = "dead-successor-sid"
        from app import redis_client as rc

        client = rc.get_client()
        # Grace row points at successor_jti, but successor's primary
        # is NOT in the kv map.
        client._kv[f"auth:session:grace:{old_jti}"] = json.dumps(
            {
                "user_id": seed["user_id"],
                "sid": sid,
                "successor_jti": successor_jti,
            },
            separators=(",", ":"),
        )
        client._sets[f"auth:session:by_sid:{sid}"].add(old_jti)
        token = _mint_token_for(seed["user_id"], jti=old_jti, sid=sid)

        app = _make_app(session_factory)
        with structlog.testing.capture_logs() as captured:
            with TestClient(app) as cli:
                res = cli.post(
                    "/api/v1/auth/refresh",
                    cookies={"refresh_token": token},
                )
        assert res.status_code == 401
        rejection_logs = [
            ev for ev in captured
            if ev.get("event") == "auth.refresh.rejected"
            and ev.get("reason") == "catchup_successor_unavailable"
        ]
        assert len(rejection_logs) == 1, (
            f"Expected one catchup_successor_unavailable; got: {captured}"
        )
        # Diagnostic flag identifies the cause for ops.
        assert rejection_logs[0]["successor_row_missing"] is True


# ── 7. /verify never emits Set-Cookie even on grace path ────────────────


class TestVerifyNeverEmitsCookie:
    @pytest.mark.asyncio
    async def test_verify_on_grace_path_does_not_setcookie(
        self, session_factory
    ) -> None:
        """RSC invariant: ``/auth/verify`` must NEVER emit Set-Cookie.
        The catch-up logic lives only in ``/auth/refresh``."""
        seed = await _seed_user(session_factory)
        old_jti = "verify-grace-old"
        successor_jti = "verify-grace-successor"
        sid = "verify-grace-sid"
        _seed_session_state(
            seed["user_id"],
            old_jti=old_jti,
            successor_jti=successor_jti,
            sid=sid,
        )
        token = _mint_token_for(seed["user_id"], jti=old_jti, sid=sid)

        app = _make_app(session_factory)
        with TestClient(app) as cli:
            res = cli.post(
                "/api/v1/auth/verify",
                cookies={"refresh_token": token},
            )
        assert res.status_code == 200
        # No Set-Cookie header at all from /verify — neither the
        # canonical Path=/ nor the legacy delete header. The validator's
        # grace branch hands back ``session_row`` but the verify handler
        # deliberately discards it.
        set_cookies = [
            v for k, v in res.headers.items() if k.lower() == "set-cookie"
        ]
        assert set_cookies == [], (
            f"/verify must never emit Set-Cookie; got {set_cookies}"
        )
