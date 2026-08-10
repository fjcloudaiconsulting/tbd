"""TBD-352 — the credentials minted by invitation-accept reactivation must
survive the very next request.

The reactivation branch of ``invitation_service.accept_invitation`` writes
``password_changed_at`` and ``sessions_invalidated_at`` from a single
``utcnow_naive()`` value carrying **microseconds**. The router then mints the
access token (``create_access_token``) and the refresh session
(``_issue_refresh_session``), and both write ``"iat": int(now.timestamp())``,
floored to a whole second.

Every validator compares with a strict ``<`` against
``security.token_cutoff(user) == max(password_changed_at, sessions_invalidated_at)``:

  * ``deps.py`` ``get_current_user``          → 401
  * ``deps.py`` ``get_current_user_optional`` → silently resolves as anonymous
  * ``auth.py`` ``/auth/refresh``             → 401 ``iat_before_cutoff``

``floor(T) < T.<microseconds>`` is true whenever the microsecond component is
non-zero, so the user is logged straight back out.

Why these fences are end-to-end: the service-layer reactivation test only
asserts the two columns are ``not None`` and never uses the returned
credential, and the existing router tests assert ``"access_token" in body``
without ever making a follow-up authenticated request. Both stay green with
the bug present.

Why the clock is frozen with a non-zero microsecond: anchored to a whole
second ``floor(T) < T.000000`` is false and every fence below is vacuous.

Why ``password_changed_at`` matters as much as ``sessions_invalidated_at``:
``token_cutoff`` takes the ``max()`` of the two, and the reactivation branch
sets both from the same value. Truncating only ``sessions_invalidated_at``
(the fix the ticket originally described) leaves ``password_changed_at``
supplying the identical cutoff and the bug survives unchanged — these fences
must, and do, stay RED against that half-fix.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app import security as security_module
from app.database import get_db
from app.deps import get_current_user_optional, get_session_factory
from app.models import Base
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers.auth import router as auth_router
from app.routers.org_members import router as org_members_router
from app.security import create_access_token, create_invitation_token, hash_password
from app.services import invitation_service

from tests.conftest import set_refresh_cookie


NEW_PASSWORD = "brand-new-pw-1234"


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


@pytest.fixture(autouse=True)
def fake_redis(_autouse_fake_redis):
    """``_issue_refresh_session`` fails closed on unreachable Redis; the
    in-process fake from ``tests/conftest.py`` stands in."""
    yield _autouse_fake_redis


def _make_app(session_factory) -> FastAPI:
    """Real routers, real ``get_current_user`` / ``get_current_user_optional``.

    Nothing about authentication is overridden — that is the whole point of
    these fences. Only the DB session is redirected at the in-memory engine.

    ``/probe/optional`` is a stand-in for any ``get_current_user_optional``
    route (``/api/v1/auth/status`` is the real one) that reports, in the
    response body, whether the caller resolved. ``/auth/status`` answers 200
    either way, so it cannot distinguish "authenticated" from "silently
    anonymous" — the exact failure mode ``deps.py`` line 124 produces.
    """
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
    app.include_router(org_members_router)

    @app.get("/probe/optional")
    async def probe_optional(user=Depends(get_current_user_optional)):
        return {"user_id": user.id if user is not None else None}

    return app


# ── frozen clock ────────────────────────────────────────────────────────────


def _freeze(monkeypatch, instant: datetime) -> datetime:
    """Pin BOTH clocks that feed the comparison to the same instant.
    Returns the frozen naive-UTC value so callers can assert against the
    exact instant the write sites are supposed to floor.

    ``invitation_service.utcnow_naive`` produces the cutoff written to the
    two user columns; ``security.datetime.now`` produces the ``iat`` stamped
    on the access + refresh tokens. Left unfrozen, whether the token is
    minted before or after the next whole-second boundary decides the result
    and the fence flakes. Frozen together, the outcome depends only on
    whether the write sites floor their value.
    """
    aware = instant if instant.tzinfo else instant.replace(tzinfo=timezone.utc)
    naive = aware.replace(tzinfo=None)

    # GUARD on the fences themselves, not on the app. Every fence in this
    # module is vacuous at a whole second: the bug is
    # ``floor(T) < T.<micros>``, which is false when micros is 0. Measured —
    # replacing the ``500_001`` literals with ``0`` makes this entire file
    # pass against unfixed ``main``. Fail loudly here rather than quietly
    # proving nothing if someone edits that constant.
    assert naive.microsecond != 0, (
        "The frozen instant must carry a non-zero microsecond component. "
        "At a whole second floor(T) < T is false and every fence in this "
        "module passes against the unfixed code."
    )

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return aware.astimezone(tz) if tz is not None else naive

        @classmethod
        def utcnow(cls):
            return naive

    monkeypatch.setattr(security_module, "datetime", _FrozenDatetime)
    monkeypatch.setattr(invitation_service, "utcnow_naive", lambda: naive)
    return naive


# Deliberately ABOVE 500_000, not merely non-zero. MySQL 8.0's default
# sql_mode rounds fractional seconds to nearest when storing into an fsp-0
# DATETIME (measured on 8.0.46), so a "round instead of floor" write site is
# only distinguishable from a flooring one when the fraction is >= .5. At
# .4 a round-to-nearest implementation coincides with flooring and the
# equality assertion below would pass against it.
CUTOFF_MICROSECONDS = 500_001


def _instant_with_microseconds(micros: int, *, shift_seconds: int = 0) -> datetime:
    """Near-real "now" (so JWT ``exp`` stays in the future) carrying an
    explicit microsecond component."""
    return (
        datetime.now(timezone.utc).replace(microsecond=micros)
        + timedelta(seconds=shift_seconds)
    )


# ── seeding ─────────────────────────────────────────────────────────────────


async def _seed_org_with_owner(factory) -> dict:
    async with factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        owner = User(
            org_id=org.id, username="owner", email="owner@acme.io",
            password_hash=hash_password("owner-pass-1234"),
            role=Role.OWNER, is_superadmin=False, is_active=True,
            email_verified=True,
        )
        db.add(owner)
        await db.commit()
        return {"org_id": org.id, "owner_id": owner.id}


async def _seed_soft_deleted_member(factory, *, org_id: int) -> int:
    async with factory() as db:
        user = User(
            org_id=org_id, username="dora", email="dora@acme.io",
            password_hash=hash_password("old-pw-1234567"),
            role=Role.MEMBER, is_superadmin=False, is_active=False,
            email_verified=True,
        )
        db.add(user)
        await db.commit()
        return user.id


async def _pending_invitation_token(factory, *, org_id: int, owner_id: int) -> str:
    async with factory() as db:
        inv = await invitation_service.create_invitation(
            db, org_id=org_id, created_by=owner_id,
            email="dora@acme.io", role=Role.MEMBER,
        )
        await db.commit()
        return create_invitation_token(inv.id, inv.email)


def _refresh_cookie_value(headers) -> str | None:
    raw_iter = headers.raw if hasattr(headers, "raw") else []
    for raw in raw_iter:
        if isinstance(raw, tuple):
            key, value = raw
            if key.decode().lower() != "set-cookie":
                continue
            value = value.decode()
        else:
            value = raw
        head = value.split(";", 1)[0].strip()
        name, _, token = head.partition("=")
        if name == "refresh_token" and "Max-Age=0" not in value and token:
            return token
    return None


async def _accept_as_reactivation(client, factory, *, org_id: int, owner_id: int):
    token = await _pending_invitation_token(factory, org_id=org_id, owner_id=owner_id)
    res = client.post(
        "/api/v1/orgs/invitations/accept",
        json={"token": token, "username": "dora", "password": NEW_PASSWORD},
    )
    assert res.status_code == 200, res.text
    return res


# ── fences ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fence_reactivation_access_token_survives_next_request(
    session_factory, monkeypatch
):
    """FENCE (TBD-352). Kills:
      (i)   no truncation at either write site;
      (ii)  truncating only ``sessions_invalidated_at`` — the ticket's own
            half-fix — because ``password_changed_at`` is set from the same
            microsecond value and ``token_cutoff`` takes the ``max()``;
      (iii) truncation moved into ``token_cutoff`` (read side) instead of the
            write site;
      (iv)  flooring PAST the intended instant — to the whole minute, or
            ``floor - 1s``, or any other over-shoot;
      (v)   rounding to nearest instead of flooring.

    Accept the invitation, then use the returned bearer token on a second,
    real request through the unmodified ``get_current_user`` dependency.

    (iv) and (v) are invisible to the behavioural assertion: a cutoff pushed
    further into the past accepts the new credential just fine. They are
    caught only by the exact-equality assertion at the bottom, which pins the
    cutoff from BOTH sides. A cutoff that floors too far leaves the
    reactivated user's PREVIOUS sessions alive, which is the whole point of
    writing a cutoff at all.
    """
    seed = await _seed_org_with_owner(session_factory)
    user_id = await _seed_soft_deleted_member(session_factory, org_id=seed["org_id"])

    frozen = _freeze(monkeypatch, _instant_with_microseconds(CUTOFF_MICROSECONDS))
    expected_cutoff = frozen.replace(microsecond=0)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        accepted = await _accept_as_reactivation(
            client, session_factory, org_id=seed["org_id"], owner_id=seed["owner_id"]
        )
        access = accepted.json()["access_token"]
        followup = client.get(
            "/api/v1/orgs/members",
            headers={"Authorization": f"Bearer {access}"},
        )

    assert followup.status_code == 200, (
        "The access token invitation-accept just minted was rejected on the "
        f"very next request: {followup.status_code} {followup.text}"
    )

    # ⚠ DO NOT WEAKEN OR DELETE — this is not redundant with the 200 above.
    #
    # Two independent jobs, neither of which any behavioural assertion in this
    # module can do:
    #
    # 1. It is the SOLE PROXY FOR MySQL fsp-0 ROUNDING, which the CI database
    #    physically cannot exercise. Both columns are fsp-0 MySQL DATETIME and
    #    MySQL 8.0 ROUNDS fractional seconds on insert (measured on 8.0.46:
    #    .500001 reads back as the NEXT whole second), so a fix that truncates
    #    on READ inside token_cutoff cannot undo a value already rounded past
    #    the token's iat — it fixes nothing in production. Under aiosqlite,
    #    where microseconds persist verbatim, that same read-side fix looks
    #    perfectly correct: measured, all three behavioural fences in this
    #    module stay GREEN against it and ONLY this assertion fires.
    #
    # 2. It pins the cutoff from BOTH sides. `microsecond == 0` alone is a
    #    one-sided cap: `.replace(microsecond=0, second=0)` (floor to the
    #    minute) and `.replace(microsecond=0) - timedelta(seconds=1)` both
    #    satisfy it while silently keeping the reactivated user's previous
    #    sessions alive. Equality against the exact expected instant is what
    #    kills them, and rounding-to-nearest with them.
    async with session_factory() as db:
        row = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        assert row.password_changed_at == expected_cutoff, (
            f"password_changed_at must be exactly {expected_cutoff} (the frozen "
            f"instant floored to the second), got {row.password_changed_at}. "
            "Too late re-opens TBD-352 via token_cutoff()'s max(); too early "
            "leaves the pre-reactivation sessions alive."
        )
        assert row.sessions_invalidated_at == expected_cutoff, (
            f"sessions_invalidated_at must be exactly {expected_cutoff}, got "
            f"{row.sessions_invalidated_at}"
        )


@pytest.mark.asyncio
async def test_fence_reactivation_refresh_cookie_survives_next_request(
    session_factory, monkeypatch
):
    """FENCE (TBD-352). Same two wrong implementations, second credential.

    ``_issue_refresh_session`` floors ``iat`` exactly like the access token,
    and ``/auth/refresh`` runs the same strict ``<`` against ``token_cutoff``
    (``_log_refresh_rejected("iat_before_cutoff")``). A user whose bearer
    token were fixed but whose refresh cookie were not would still be
    logged out at the first silent refresh.
    """
    seed = await _seed_org_with_owner(session_factory)
    await _seed_soft_deleted_member(session_factory, org_id=seed["org_id"])

    _freeze(monkeypatch, _instant_with_microseconds(CUTOFF_MICROSECONDS))

    app = _make_app(session_factory)
    with TestClient(app) as client:
        accepted = await _accept_as_reactivation(
            client, session_factory, org_id=seed["org_id"], owner_id=seed["owner_id"]
        )
        refresh_token = _refresh_cookie_value(accepted.headers)
        assert refresh_token, "invite-accept must emit a refresh_token cookie"
        set_refresh_cookie(client, refresh_token)
        rotated = client.post("/api/v1/auth/refresh")

    assert rotated.status_code == 200, (
        "The refresh cookie invitation-accept just set was rejected on its "
        f"first use: {rotated.status_code} {rotated.text}"
    )


@pytest.mark.asyncio
async def test_fence_reactivated_user_is_not_anonymous_on_optional_auth(
    session_factory, monkeypatch
):
    """FENCE (TBD-352, ``deps.py`` line 124). Same two wrong implementations
    on the third validator, which the ticket missed.

    ``get_current_user_optional`` runs the identical strict ``<`` but returns
    ``None`` instead of raising, so a reactivated user resolves as anonymous
    with no 401 anywhere to notice. A 200 alone proves nothing here — the
    body must name the user.
    """
    seed = await _seed_org_with_owner(session_factory)
    user_id = await _seed_soft_deleted_member(session_factory, org_id=seed["org_id"])

    _freeze(monkeypatch, _instant_with_microseconds(CUTOFF_MICROSECONDS))

    app = _make_app(session_factory)
    with TestClient(app) as client:
        accepted = await _accept_as_reactivation(
            client, session_factory, org_id=seed["org_id"], owner_id=seed["owner_id"]
        )
        access = accepted.json()["access_token"]
        probe = client.get(
            "/probe/optional", headers={"Authorization": f"Bearer {access}"}
        )

    assert probe.status_code == 200, probe.text
    assert probe.json()["user_id"] == user_id, (
        "Reactivated user resolved as anonymous on the optional-auth path "
        "(deps.get_current_user_optional swallowed the cutoff rejection)"
    )


# ── guards ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_guard_stale_pre_reactivation_token_is_still_rejected(
    session_factory, monkeypatch
):
    """GUARD (over-reach fence, TBD-352). The cutoff must still do its job.

    Without this, a "fix" that simply stopped writing the cutoff at all
    would pass every fence above while quietly disabling session
    invalidation on reactivation. A token minted a minute before the accept
    must be dead on both the strict (401) and the optional (anonymous) path.
    """
    seed = await _seed_org_with_owner(session_factory)
    user_id = await _seed_soft_deleted_member(session_factory, org_id=seed["org_id"])

    # Mint the pre-existing session token a full minute before the accept.
    _freeze(monkeypatch, _instant_with_microseconds(CUTOFF_MICROSECONDS, shift_seconds=-60))
    stale_access = create_access_token(user_id, seed["org_id"], Role.MEMBER.value)

    _freeze(monkeypatch, _instant_with_microseconds(CUTOFF_MICROSECONDS))

    app = _make_app(session_factory)
    with TestClient(app) as client:
        await _accept_as_reactivation(
            client, session_factory, org_id=seed["org_id"], owner_id=seed["owner_id"]
        )
        strict = client.get(
            "/api/v1/orgs/members",
            headers={"Authorization": f"Bearer {stale_access}"},
        )
        optional = client.get(
            "/probe/optional", headers={"Authorization": f"Bearer {stale_access}"}
        )

    assert strict.status_code == 401, (
        "Reactivation must still kill sessions issued before it: the stale "
        f"token was accepted ({strict.status_code})"
    )
    assert strict.json()["detail"] == "Session has been invalidated"
    assert optional.json()["user_id"] is None


@pytest.mark.asyncio
async def test_guard_reactivation_leaves_the_rest_of_the_row_alone(
    session_factory, monkeypatch
):
    """GUARD (control, TBD-352). An ordinary member round-trips correctly and
    nothing beyond the intended fields moved. Also pins that the cutoff is
    still written at all (both columns non-null) — flooring must not become
    "skip"."""
    seed = await _seed_org_with_owner(session_factory)
    user_id = await _seed_soft_deleted_member(session_factory, org_id=seed["org_id"])

    _freeze(monkeypatch, _instant_with_microseconds(CUTOFF_MICROSECONDS))

    app = _make_app(session_factory)
    with TestClient(app) as client:
        await _accept_as_reactivation(
            client, session_factory, org_id=seed["org_id"], owner_id=seed["owner_id"]
        )

    async with session_factory() as db:
        row = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        assert row.id == user_id
        assert row.username == "dora"
        assert row.email == "dora@acme.io"
        assert row.role == Role.MEMBER
        assert row.is_active is True
        assert row.email_verified is True
        assert row.is_superadmin is False
        assert row.password_changed_at is not None
        assert row.sessions_invalidated_at is not None
