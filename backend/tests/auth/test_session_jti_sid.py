"""PR 2 — Refresh ``jti`` + ``sid`` + session family rows tests.

Pins every architect-emphasized review risk in
``specs/2026-05-17-backend-session-model.md`` §8 PR 2:

1. New refresh JWT carries both ``jti`` and ``sid`` at every issue site
   (login, ``/refresh`` rotation, MFA branches via ``_issue_tokens``,
   Google callback, ``org_members.py`` invitation accept).
2. ``sid`` is preserved across the rotation chain; only ``jti`` changes.
3. Every issue site writes the family row AND its first member to the
   session store BEFORE emitting the cookie.
4. Legacy (no-jti or no-sid) refresh JWTs are rejected with 401
   ``"Session has been invalidated"``.
5. Deleting the session family produces 401 on next ``/refresh``.
6. Family membership matches the issued ``jti`` chain after
   rotation.
7. Session store unreachable => 503 on every issue path, no Set-Cookie emitted.
8. Grep-style guard: every ``create_refresh_token`` call site is
   co-located with a paired session-store write within the same source file.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import jwt
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
from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_session_factory
from app.models import Base
from app.models.subscription import Plan
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers import auth as auth_module
from app.routers.auth import LEGACY_REFRESH_COOKIE_PATH, router as auth_router
from app.routers.org_members import router as org_members_router
from app.security import (
    create_invitation_token,
    create_mfa_challenge_token,
    decode_refresh_jti_sid,
    hash_password,
)
from app.services.mfa_service import (
    generate_recovery_codes,
    hash_recovery_code,
)

from tests.conftest import set_refresh_cookie, state_jtis


PASSWORD = "starting-password-1"


# ── DB fixture ──────────────────────────────────────────────────────────────


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
    app.include_router(org_members_router)
    return app


async def _seed_user(
    factory: async_sessionmaker[AsyncSession],
    *,
    mfa_enabled: bool = False,
    recovery_codes_plaintext: list[str] | None = None,
) -> dict:
    async with factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        recovery_field: str | None = None
        if recovery_codes_plaintext is not None:
            recovery_field = ",".join(
                hash_recovery_code(c) for c in recovery_codes_plaintext
            )
        user = User(
            org_id=org.id,
            username="alice",
            email="alice@example.com",
            password_hash=hash_password(PASSWORD),
            role=Role.OWNER,
            is_superadmin=False,
            is_active=True,
            email_verified=True,
            mfa_enabled=mfa_enabled,
            recovery_codes=recovery_field,
        )
        db.add(user)
        await db.commit()
        return {"org_id": org.id, "user_id": user.id}


async def _seed_default_plan(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as db:
        existing = await db.scalar(select(Plan).where(Plan.slug == "free"))
        if existing is None:
            db.add(Plan(slug="free", name="Free", is_active=True, sort_order=0))
            await db.commit()


# ── Set-Cookie parsing helpers ──────────────────────────────────────────────


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
    """Extract the JWT value from a Set-Cookie header."""
    head = raw.split(";", 1)[0].strip()
    name, _, value = head.partition("=")
    assert name == "refresh_token"
    return value


# ── Google SSO httpx mock ───────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def _patch_httpx(monkeypatch, *, userinfo_email: str) -> None:
    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def post(self, *args, **kwargs):
            return _FakeResponse(200, {"access_token": "fake-google-token"})

        async def get(self, *args, **kwargs):
            return _FakeResponse(
                200,
                {
                    "email": userinfo_email,
                    "verified_email": True,
                    "given_name": "Existing",
                    "family_name": "User",
                },
            )

    monkeypatch.setattr(auth_module.httpx, "AsyncClient", _FakeClient)


@pytest.fixture
def google_config(monkeypatch):
    monkeypatch.setattr(app_settings, "google_client_id", "test-client-id")
    monkeypatch.setattr(app_settings, "google_client_secret", "test-client-secret")
    monkeypatch.setattr(app_settings, "app_url", "http://localhost")
    yield


# ── 1. Every issue site stamps jti + sid in the JWT AND in the store ──────────


def _decode_unverified(token: str) -> dict:
    return jwt.decode(
        token, app_settings.jwt_secret_key, algorithms=[app_settings.jwt_algorithm]
    )


@pytest.mark.asyncio
async def test_login_password_branch_writes_primary_and_family(
    session_factory
) -> None:
    """Login password branch: JWT carries jti+sid AND the store has the
    family and its first member before the cookie is set."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)

    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": PASSWORD},
        )

    assert res.status_code == 200, res.json()
    raw = _canonical_refresh_cookie(res.headers)
    assert raw is not None, "login must set canonical refresh_token cookie"
    token = _refresh_token_from_set_cookie(raw)
    payload = _decode_unverified(token)
    assert payload.get("jti"), "refresh JWT must carry jti claim"
    assert payload.get("sid"), "refresh JWT must carry sid claim"

    assert state_db._validate(payload["jti"]) is not None
    assert payload["jti"] in state_jtis(payload["sid"])


@pytest.mark.asyncio
async def test_refresh_rotation_preserves_sid(session_factory) -> None:
    """``/refresh`` rotation: new JWT has new jti but SAME sid; new
    new jti is the head, new jti is in the family."""
    seed = await _seed_user(session_factory)
    # Establish a session through the real login flow so the predecessor
    # JWT has the new shape (jti + sid).
    app = _make_app(session_factory)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": PASSWORD},
        )
        login_raw = _canonical_refresh_cookie(login.headers)
        login_token = _refresh_token_from_set_cookie(login_raw)
        original_jti, original_sid = decode_refresh_jti_sid(login_token)

        set_refresh_cookie(client, login_token)
        res = client.post(
            "/api/v1/auth/refresh"
        )

    assert res.status_code == 200, res.json()
    raw = _canonical_refresh_cookie(res.headers)
    assert raw is not None
    new_token = _refresh_token_from_set_cookie(raw)
    new_jti, new_sid = decode_refresh_jti_sid(new_token)

    assert new_jti != original_jti, "rotation must mint a fresh jti"
    assert new_sid == original_sid, "rotation must preserve the family sid"

    # New head present, old one no longer the head.
    assert state_db._validate(new_jti) is not None
    assert state_db._validate(original_jti) is None
    # Family carries the new jti.
    assert new_jti in state_jtis(new_sid)
    _ = seed


@pytest.mark.asyncio
async def test_sid_preserved_across_five_rotations(
    session_factory
) -> None:
    """The architect-pinned 5-rotation invariant: every rotation issues a
    fresh jti but reuses the original sid verbatim."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)

    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": PASSWORD},
        )
        token = _refresh_token_from_set_cookie(_canonical_refresh_cookie(login.headers))
        original_jti, original_sid = decode_refresh_jti_sid(token)

        seen_jtis = [original_jti]
        for _ in range(5):
            set_refresh_cookie(client, token)
            res = client.post(
                "/api/v1/auth/refresh"
            )
            assert res.status_code == 200, res.json()
            raw = _canonical_refresh_cookie(res.headers)
            token = _refresh_token_from_set_cookie(raw)
            new_jti, new_sid = decode_refresh_jti_sid(token)
            assert new_sid == original_sid, (
                f"sid drifted on rotation: {new_sid!r} != {original_sid!r}"
            )
            assert new_jti not in seen_jtis, "jti must rotate every refresh"
            seen_jtis.append(new_jti)

    # Last successor is the live head.
    assert state_db._validate(seen_jtis[-1]) is not None


@pytest.mark.asyncio
async def test_mfa_recovery_branch_writes_primary_and_family(
    session_factory
) -> None:
    """MFA recovery branch (one of the ``_issue_tokens`` callers) stamps
    jti + sid and writes the family."""
    codes = generate_recovery_codes(count=3)
    seed = await _seed_user(
        session_factory,
        mfa_enabled=True,
        recovery_codes_plaintext=codes,
    )
    mfa_token = create_mfa_challenge_token(seed["user_id"])
    app = _make_app(session_factory)

    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/mfa/recovery",
            json={"mfa_token": mfa_token, "code": codes[0]},
        )
    assert res.status_code == 200, res.json()
    raw = _canonical_refresh_cookie(res.headers)
    assert raw is not None
    token = _refresh_token_from_set_cookie(raw)
    jti, sid = decode_refresh_jti_sid(token)
    assert state_db._validate(jti) is not None
    assert jti in state_jtis(sid)


@pytest.mark.asyncio
async def test_google_callback_writes_primary_and_family(
    session_factory, google_config, monkeypatch
) -> None:
    """Google SSO callback (fifth issue site) stamps jti + sid and writes
    the family before its RedirectResponse goes out."""
    await _seed_default_plan(session_factory)
    _patch_httpx(monkeypatch, userinfo_email="brand-new-sso@example.com")
    app = _make_app(session_factory)

    with TestClient(app) as client:
        client.cookies.set("oauth_state", "matching-state")
        res = client.get(
            "/api/v1/auth/google/callback",
            params={"code": "dummy", "state": "matching-state"},
            follow_redirects=False,
        )

    assert res.status_code == 302, res.text
    raw = _canonical_refresh_cookie(res.headers)
    assert raw is not None
    token = _refresh_token_from_set_cookie(raw)
    jti, sid = decode_refresh_jti_sid(token)
    assert state_db._validate(jti) is not None
    assert jti in state_jtis(sid)


@pytest.mark.asyncio
async def test_invitation_accept_writes_primary_and_family(
    session_factory
) -> None:
    """``routers/org_members.py`` invitation accept (the issue site PR 1
    missed) stamps jti + sid and writes the family."""
    from app.services import invitation_service

    # Seed org + owner so the invitation belongs to a real org.
    async with session_factory() as db:
        org = Organization(name="Inv Co", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        owner = User(
            org_id=org.id,
            username="owner",
            email="owner@inv.io",
            password_hash=hash_password(PASSWORD),
            role=Role.OWNER,
            is_superadmin=False,
            is_active=True,
            email_verified=True,
        )
        db.add(owner)
        await db.commit()
        org_id, owner_id = org.id, owner.id

    async with session_factory() as db:
        inv = await invitation_service.create_invitation(
            db,
            org_id=org_id,
            created_by=owner_id,
            email="invitee@inv.io",
            role=Role.MEMBER,
        )
        await db.commit()
        token = create_invitation_token(inv.id, inv.email)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/orgs/invitations/accept",
            json={
                "token": token,
                "username": "invitee",
                "password": "strong-pw-1234",
            },
        )

    assert res.status_code == 200, res.text
    raw = _canonical_refresh_cookie(res.headers)
    assert raw is not None
    refresh = _refresh_token_from_set_cookie(raw)
    jti, sid = decode_refresh_jti_sid(refresh)
    assert state_db._validate(jti) is not None
    assert jti in state_jtis(sid)


# ── 2. Legacy tokens rejected ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_legacy_no_jti_no_sid_token_rejected(
    session_factory
) -> None:
    """A pre-PR2 refresh JWT (no jti, no sid) is rejected with 401
    ``Session has been invalidated`` — the planned reauth break."""
    seed = await _seed_user(session_factory)
    # Hand-craft a token in the OLD shape (no jti, no sid).
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    legacy_payload = {
        "sub": str(seed["user_id"]),
        "type": "refresh",
        "session_created_at": now.timestamp(),
        "iat": int(now.timestamp()),
        "exp": now + timedelta(days=app_settings.session_lifetime_days),
    }
    legacy_token = jwt.encode(
        legacy_payload,
        app_settings.jwt_secret_key,
        algorithm=app_settings.jwt_algorithm,
    )

    app = _make_app(session_factory)
    with TestClient(app) as client:
        set_refresh_cookie(client, legacy_token)
        res = client.post(
            "/api/v1/auth/refresh"
        )

    assert res.status_code == 401, res.json()
    assert res.json()["detail"] == "Session has been invalidated"


@pytest.mark.asyncio
async def test_manual_family_delete_invalidates_session(
    session_factory
) -> None:
    """Deleting the session family produces 401 on next /refresh
    — the per-session-revocation primitive."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": PASSWORD},
        )
        token = _refresh_token_from_set_cookie(_canonical_refresh_cookie(login.headers))
        jti, sid = decode_refresh_jti_sid(token)

        # Operator yanks the family out of the store.
        state_db._revoke_family(sid)

        set_refresh_cookie(client, token)
        res = client.post(
            "/api/v1/auth/refresh"
        )

    assert res.status_code == 401, res.json()
    assert res.json()["detail"] == "Session has been invalidated"


@pytest.mark.asyncio
async def test_family_set_membership_matches_rotation_chain(
    session_factory
) -> None:
    """After N rotations the family holds every issued jti (rotation never
    removes members; the revoke-by-sid path deletes the whole family)."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": PASSWORD},
        )
        token = _refresh_token_from_set_cookie(_canonical_refresh_cookie(login.headers))
        first_jti, sid = decode_refresh_jti_sid(token)
        issued = [first_jti]

        for _ in range(3):
            set_refresh_cookie(client, token)
            res = client.post(
                "/api/v1/auth/refresh"
            )
            assert res.status_code == 200
            token = _refresh_token_from_set_cookie(_canonical_refresh_cookie(res.headers))
            jti, _ = decode_refresh_jti_sid(token)
            issued.append(jti)

    # Every jti ever issued for this sid sits in the family.
    assert set(issued).issubset(
        state_jtis(sid)
    ), "family must accumulate every issued jti"


# ── 3. Session store unreachable => 503 at every issue site ─────────────────────────


@pytest.mark.asyncio
async def test_login_503_when_store_unreachable(
    session_factory, state_db_down
) -> None:
    """Session store unreachable at login => 503, NO Set-Cookie."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": PASSWORD},
        )
    assert res.status_code == 503, res.json()
    assert _canonical_refresh_cookie(res.headers) is None


@pytest.mark.asyncio
async def test_refresh_503_when_store_unreachable(
    session_factory, request
) -> None:
    """Session store unreachable at /refresh => 503, NO Set-Cookie."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": PASSWORD},
        )
        token = _refresh_token_from_set_cookie(_canonical_refresh_cookie(login.headers))

    # Now make the store disappear and try to rotate.
    request.getfixturevalue("state_db_down")
    with TestClient(app) as client:
        set_refresh_cookie(client, token)
        res = client.post(
            "/api/v1/auth/refresh"
        )
    assert res.status_code == 503, res.json()
    assert _canonical_refresh_cookie(res.headers) is None


@pytest.mark.asyncio
async def test_google_callback_503_when_store_unreachable(
    session_factory, google_config, monkeypatch, state_db_down
) -> None:
    """Google SSO callback fails closed when the session store is unreachable."""
    await _seed_default_plan(session_factory)
    _patch_httpx(monkeypatch, userinfo_email="brand-new-sso@example.com")
    app = _make_app(session_factory)
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "matching-state")
        res = client.get(
            "/api/v1/auth/google/callback",
            params={"code": "dummy", "state": "matching-state"},
            follow_redirects=False,
        )
    # The callback would normally return 302; on store failure it raises
    # 503 from inside _issue_refresh_session. The handler does not
    # special-case it, so FastAPI emits the 503 JSON.
    assert res.status_code == 503, res.text
    assert _canonical_refresh_cookie(res.headers) is None


@pytest.mark.asyncio
async def test_mfa_recovery_503_when_store_unreachable(
    session_factory, state_db_down
) -> None:
    """MFA recovery (one of the _issue_tokens callers) fails closed."""
    codes = generate_recovery_codes(count=3)
    seed = await _seed_user(
        session_factory,
        mfa_enabled=True,
        recovery_codes_plaintext=codes,
    )
    mfa_token = create_mfa_challenge_token(seed["user_id"])
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/mfa/recovery",
            json={"mfa_token": mfa_token, "code": codes[0]},
        )
    assert res.status_code == 503, res.json()
    assert _canonical_refresh_cookie(res.headers) is None


@pytest.mark.asyncio
async def test_invitation_accept_503_when_store_unreachable(
    session_factory, state_db_down
) -> None:
    """org_members.py invitation accept fails closed — the architect
    explicitly enumerated this as the missed fifth site."""
    from app.services import invitation_service

    async with session_factory() as db:
        org = Organization(name="Inv Co", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        owner = User(
            org_id=org.id,
            username="owner",
            email="owner@inv.io",
            password_hash=hash_password(PASSWORD),
            role=Role.OWNER,
            is_superadmin=False,
            is_active=True,
            email_verified=True,
        )
        db.add(owner)
        await db.commit()
        org_id, owner_id = org.id, owner.id

    async with session_factory() as db:
        inv = await invitation_service.create_invitation(
            db,
            org_id=org_id,
            created_by=owner_id,
            email="invitee@inv.io",
            role=Role.MEMBER,
        )
        await db.commit()
        token = create_invitation_token(inv.id, inv.email)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/orgs/invitations/accept",
            json={
                "token": token,
                "username": "invitee",
                "password": "strong-pw-1234",
            },
        )
    assert res.status_code == 503, res.text
    assert _canonical_refresh_cookie(res.headers) is None


# ── 4. Grep-style guard: every create_refresh_token site has a store write ──


def test_every_create_refresh_token_site_pairs_with_store_write() -> None:
    """Pin the architect's structural defense: every file that calls
    ``create_refresh_token`` must also call ``session_issue`` (or
    ``session_rotate``) within the same file. If a future PR adds a
    new issue site without the store write, this test fails loudly.

    Mirrors ``test_no_hardcoded_seven_day_refresh_cookie_literals_remain``
    in shape — guard tests beat code review for this class of trap.
    """
    app_dir = Path(__file__).resolve().parents[2] / "app"
    offenders: list[str] = []
    for py in app_dir.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        # Skip the helpers themselves — security.py DEFINES the function;
        # state_db.py implements session_issue/session_rotate.
        rel = py.relative_to(app_dir.parent)
        if py.name in {"security.py", "state_db.py"}:
            continue
        if "create_refresh_token(" not in text:
            continue
        # The function must be paired with a session_issue / session_rotate
        # call OR with a wrapper that does so. ``routers/auth.py`` defines
        # ``_issue_refresh_session`` / ``_rotate_refresh_session`` which
        # are the in-router wrappers; both expand to the store writes.
        # ``routers/org_members.py`` calls ``_issue_refresh_session``.
        pairing_signals = (
            "session_issue",
            "session_rotate",
            "_issue_refresh_session",
            "_rotate_refresh_session",
        )
        if not any(sig in text for sig in pairing_signals):
            offenders.append(
                f"{rel}: calls create_refresh_token but does not invoke "
                "session_issue / session_rotate / _issue_refresh_session / "
                "_rotate_refresh_session"
            )
    assert offenders == [], (
        "Every create_refresh_token call site must pair with the session "
        "store write before the cookie is set "
        "(specs/2026-05-17-backend-session-model.md §5.4). Offenders: "
        + "; ".join(offenders)
    )


# ── 5. /verify accepts a valid jti + sid token ──────────────────────────────


@pytest.mark.asyncio
async def test_verify_accepts_session_with_store_row(
    session_factory
) -> None:
    """``/auth/verify`` shares the same validation chain as ``/refresh``
    so the store probe lands automatically — pin it explicitly so a
    future refactor cannot bypass."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": PASSWORD},
        )
        token = _refresh_token_from_set_cookie(_canonical_refresh_cookie(login.headers))

        set_refresh_cookie(client, token)
        res = client.post(
            "/api/v1/auth/verify"
        )
    assert res.status_code == 200, res.json()
    # /verify must NEVER emit Set-Cookie (RSC contract).
    assert _canonical_refresh_cookie(res.headers) is None


@pytest.mark.asyncio
async def test_verify_rejects_token_with_missing_store_row(
    session_factory
) -> None:
    """``/auth/verify`` rejects a JWT whose family has been wiped."""
    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": PASSWORD},
        )
        token = _refresh_token_from_set_cookie(_canonical_refresh_cookie(login.headers))
        jti, sid = decode_refresh_jti_sid(token)
        state_db._revoke_family(sid)

        set_refresh_cookie(client, token)
        res = client.post(
            "/api/v1/auth/verify"
        )
    assert res.status_code == 401, res.json()


# ── Architect P1 (PR #306 re-review): store failure must not leave durable
#    one-time state committed without a session. ────────────────────────────


@pytest.mark.asyncio
async def test_invitation_accept_503_leaves_invitation_unconsumed(
    session_factory, state_db_down
) -> None:
    """Architect P1.1 on PR #306: invitation accept used to commit the
    invitation BEFORE calling ``_issue_refresh_session``. A store 503
    therefore returned an error to the user while the invitation was
    already marked accepted — permanent lockout, no retry possible.

    After the fix: ``accept_invitation`` flushes (so ``user.id`` is
    available for the JWT), the store write runs, and ONLY THEN
    ``db.commit()`` fires. A 503 must leave the invitation row with
    ``accepted_at IS NULL`` so the invitee can retry.
    """
    from app.models.invitation import Invitation
    from app.services import invitation_service

    async with session_factory() as db:
        org = Organization(name="Inv Co", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        owner = User(
            org_id=org.id, username="owner", email="owner@inv.io",
            password_hash=hash_password(PASSWORD),
            role=Role.OWNER, is_superadmin=False, is_active=True,
            email_verified=True,
        )
        db.add(owner)
        await db.commit()
        org_id, owner_id = org.id, owner.id

    async with session_factory() as db:
        inv = await invitation_service.create_invitation(
            db, org_id=org_id, created_by=owner_id,
            email="invitee@inv.io", role=Role.MEMBER,
        )
        await db.commit()
        inv_id = inv.id
        token = create_invitation_token(inv.id, inv.email)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/orgs/invitations/accept",
            json={
                "token": token,
                "username": "invitee",
                "password": "strong-pw-1234",
            },
        )
    assert res.status_code == 503, res.text

    # The invitation MUST still be unconsumed — invitee can retry.
    async with session_factory() as db:
        row = await db.get(Invitation, inv_id)
        assert row is not None
        assert row.accepted_at is None, (
            "Architect P1 regression: invitation was marked accepted "
            "despite the 503 — store failure must not consume one-time state"
        )
        # And no user row should have been created.
        any_user = await db.scalar(
            select(User).where(User.email == "invitee@inv.io")
        )
        assert any_user is None, (
            "Architect P1 regression: user row was committed despite 503"
        )


@pytest.mark.asyncio
async def test_google_callback_503_does_not_commit_new_user(
    session_factory, monkeypatch, google_config, state_db_down
) -> None:
    """Architect P1.2 on PR #306: first-run Google SSO used to commit
    the new user + trial BEFORE the store-backed session-issue. A 503
    therefore created the user durably but returned an error; on retry
    the user was treated as EXISTING (no ``created_user=true``), so
    the first-run privacy disclosure (Team E) was silently skipped.

    After the fix: user + trial are flushed but not committed; on a
    503 the transaction rolls back, and the next Google SSO attempt
    correctly re-enters the new-user branch.
    """
    await _seed_default_plan(session_factory)
    _patch_httpx(monkeypatch, userinfo_email="brand-new-sso@example.com")
    app = _make_app(session_factory)
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "matching-state")
        res = client.get(
            "/api/v1/auth/google/callback",
            params={"code": "dummy", "state": "matching-state"},
            follow_redirects=False,
        )
    # Google callback returns a RedirectResponse on success and an
    # error redirect on failure. The 503 path here surfaces as the
    # explicit error handler — but the critical invariant for this
    # test is the DB state: no committed user / org.
    async with session_factory() as db:
        any_user = await db.scalar(
            select(User).where(User.email == "brand-new-sso@example.com")
        )
        assert any_user is None, (
            "Architect P1 regression: new SSO user was committed "
            "despite the store 503 — next SSO attempt would skip the "
            "first-run disclosure branch"
        )


@pytest.mark.asyncio
async def test_mfa_recovery_503_preserves_recovery_code(
    session_factory, state_db_down
) -> None:
    """Architect P1.3 on PR #306: MFA recovery used to commit the
    consumed code BEFORE issuing the store-backed session. A 503
    burned one of the user's finite recovery codes without giving
    them a session, forcing them to burn another on retry. After
    the fix the commit happens after the store confirms — on a 503 the
    transaction rolls back and the code is still usable.
    """
    from app.security import create_mfa_challenge_token
    from app.services.mfa_service import (
        generate_recovery_codes,
        hash_recovery_code,
    )

    codes = generate_recovery_codes(count=3)
    code = codes[0]
    seed = await _seed_user(
        session_factory,
        mfa_enabled=True,
        recovery_codes_plaintext=codes,
    )
    mfa_token = create_mfa_challenge_token(seed["user_id"])

    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/mfa/recovery",
            json={"mfa_token": mfa_token, "code": code},
        )
    assert res.status_code == 503, res.text

    # The recovery code MUST still be usable.
    async with session_factory() as db:
        user = await db.scalar(select(User).where(User.id == seed["user_id"]))
        assert user is not None
        assert user.recovery_codes is not None
        stored_hashes = user.recovery_codes.split(",")
        # The hash of the would-be-consumed code is still present.
        expected_hash = hash_recovery_code(code)
        assert expected_hash in stored_hashes, (
            "Architect P1 regression: recovery code was consumed "
            "despite the 503 — must roll back the DB change too"
        )
        # Still have all three codes (none were burned).
        assert len(stored_hashes) == 3, stored_hashes


# ── Architect P2 (PR #306 re-review): the stored family must bind back to JWT
#    claims, not just exist. ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_rejects_jti_with_mismatched_user_id_in_store(
    session_factory
) -> None:
    """Architect P2 on PR #306: existence of the jti is not sufficient —
    the family row stores ``user_id`` and ``sid`` precisely so the
    resolver can verify the JWT still binds to it. Forge the row to carry
    a different user_id; the refresh must reject as invalidated/corrupt,
    NOT happily accept and rotate.
    """
    from sqlalchemy import update

    await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": PASSWORD},
        )
        token = _refresh_token_from_set_cookie(_canonical_refresh_cookie(login.headers))
        jti, sid = decode_refresh_jti_sid(token)

        # Forge the family row to point at a different user_id (simulates
        # corruption / overwrite / impossible jti collision).
        with state_db._engine.begin() as c:
            c.execute(
                update(state_db._F).where(state_db._F.c.sid == sid).values(user_id=999999)
            )

        set_refresh_cookie(client, token)
        res = client.post(
            "/api/v1/auth/refresh"
        )
    assert res.status_code == 401, res.json()
    assert "invalidated" in res.json()["detail"].lower()


@pytest.mark.asyncio
async def test_refresh_rejects_jti_with_mismatched_sid_in_store(
    session_factory
) -> None:
    """Architect P2 on PR #306 — sister case to the user_id mismatch.
    The JWT carries one ``sid``, the stored family carries a different
    ``sid``. Could arise from a leaked refresh cookie reused after the
    session family was reissued under a fresh ``sid``, or from
    corruption. Resolver must reject.
    """
    from app.security import create_refresh_token

    seed = await _seed_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": PASSWORD},
        )
        token = _refresh_token_from_set_cookie(_canonical_refresh_cookie(login.headers))
        jti, sid = decode_refresh_jti_sid(token)

        # Same user_id (good) and jti, but a different sid (bad).
        token, _, _ = create_refresh_token(
            seed["user_id"], sid="deadbeef-not-the-real-sid", jti=jti
        )

        set_refresh_cookie(client, token)
        res = client.post(
            "/api/v1/auth/refresh"
        )
    assert res.status_code == 401, res.json()
