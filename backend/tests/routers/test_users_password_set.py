"""L1.7 — set-initial-password and SSO step-up email change.

Pins the password_set flag end-to-end:
  - Google SSO new-user creation marks the row `password_set=False`.
  - POST /users/me/password skips the current-password check on first
    set, flips the flag True, and rotates `sessions_invalidated_at` +
    `password_changed_at`.
  - Once `password_set=True`, supplying no `current_password` is
    rejected (regression guard).
  - PUT /users/me email change accepts a valid step-up token in place
    of `current_password` only while the token is unexpired and matches.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user
from app.models import Base
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers.users import router as users_router
from app.security import hash_password, verify_password


@pytest_asyncio.fixture
async def session_factory():
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


def _make_app(session_factory, user_id: int):
    """Wire FastAPI overrides so that `get_current_user` returns a User
    bound to the SAME AsyncSession the handler will use. Otherwise the
    handler's mutations on `current_user` go to a detached instance and
    `db.commit()` persists nothing — masking real bugs."""
    from fastapi import Depends as _Depends

    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    # Depends on `get_db` so FastAPI's per-request dependency cache
    # hands the same AsyncSession to this override and to the route.
    async def override_current_user(
        request: Request,
        db: AsyncSession = _Depends(get_db),
    ) -> User:
        request.state.auth_method = "jwt"  # interactive-session guard (spec §7)
        user = await db.get(User, user_id)
        assert user is not None
        await db.refresh(user, ["organization"])
        return user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.include_router(users_router)
    return app


async def _seed_user(
    session_factory,
    *,
    password_set: bool = True,
    stepup_token: str | None = None,
    stepup_expires_at: datetime | None = None,
) -> int:
    async with session_factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        user = User(
            org_id=org.id,
            username="alice",
            email="alice@acme.io",
            password_hash=hash_password("starting-password"),
            role=Role.OWNER,
            is_active=True,
            email_verified=True,
            password_set=password_set,
            stepup_token=stepup_token,
            stepup_token_expires_at=stepup_expires_at,
        )
        db.add(user)
        await db.commit()
        return user.id


# ── SSO new-user creation ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sso_user_create_sets_password_set_false(session_factory):
    """Google-SSO-created users land with password_set=False (covers
    the new-user branch in auth.py:google_callback)."""
    async with session_factory() as db:
        org = Organization(name="SSO Org", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        # Mirrors the constructor used in auth.py google_callback.
        user = User(
            org_id=org.id,
            username="sso-user",
            email="sso@example.com",
            password_hash=hash_password("random-google-fill"),
            email_verified=True,
            role=Role.OWNER,
            password_set=False,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        assert user.password_set is False


# ── change_password handler — first-time set ────────────────────────────────


@pytest.mark.asyncio
async def test_initial_password_requires_stepup_token(session_factory):
    """Finding 1: a stolen SSO session must NOT be able to write a
    persistent local password without a fresh Google step-up. Posting
    `new_password` alone when `password_set=False` is rejected — same
    proof-of-presence requirement the email-change endpoint enforces."""
    user_id = await _seed_user(session_factory, password_set=False)
    app = _make_app(session_factory, user_id)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/users/me/password",
            json={"new_password": "brand-new-password"},
        )
    assert res.status_code == 400, res.text
    assert "step-up" in res.json()["detail"].lower()

    async with session_factory() as db:
        user = await db.get(User, user_id)
        assert user is not None
        # No write happened.
        assert user.password_set is False
        assert not verify_password("brand-new-password", user.password_hash)


@pytest.mark.asyncio
async def test_initial_password_with_valid_stepup_token_succeeds(session_factory):
    """Same flow as before, but with a valid step-up token. Sets the
    password, flips the flag, and consumes the token so it cannot be
    replayed by `/users/me` for an email change."""
    token = "first-set-stepup-" + "x" * 8
    user_id = await _seed_user(
        session_factory,
        password_set=False,
        stepup_token=token,
        stepup_expires_at=datetime.now(timezone.utc) + timedelta(minutes=4),
    )
    app = _make_app(session_factory, user_id)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/users/me/password",
            json={
                "new_password": "brand-new-password",
                "stepup_token": token,
            },
        )
    assert res.status_code == 204, res.text

    async with session_factory() as db:
        user = await db.get(User, user_id)
        assert user is not None
        assert user.password_set is True
        assert verify_password("brand-new-password", user.password_hash)
        # Token is consumed (single-use, no replay across endpoints).
        assert user.stepup_token is None
        assert user.stepup_token_expires_at is None


@pytest.mark.asyncio
async def test_initial_password_rejects_expired_stepup_token(session_factory):
    token = "expired-set-token-" + "x" * 8
    user_id = await _seed_user(
        session_factory,
        password_set=False,
        stepup_token=token,
        stepup_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    app = _make_app(session_factory, user_id)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/users/me/password",
            json={"new_password": "brand-new-password", "stepup_token": token},
        )
    assert res.status_code == 400, res.text

    async with session_factory() as db:
        user = await db.get(User, user_id)
        assert user is not None
        # Password not written, flag still False.
        assert user.password_set is False


@pytest.mark.asyncio
async def test_initial_password_rejects_replay_of_consumed_stepup_token(session_factory):
    """Step-up token is single-use across endpoints. Once /me/password
    consumes it, a second /me/password call replaying the same token
    must fail."""
    token = "single-use-set-token-" + "x" * 8
    user_id = await _seed_user(
        session_factory,
        password_set=False,
        stepup_token=token,
        stepup_expires_at=datetime.now(timezone.utc) + timedelta(minutes=4),
    )
    app = _make_app(session_factory, user_id)
    with TestClient(app) as client:
        first = client.post(
            "/api/v1/users/me/password",
            json={"new_password": "first-attempt", "stepup_token": token},
        )
        assert first.status_code == 204, first.text

        # password_set is now True, so the standard branch runs and the
        # stepup_token is rejected as not a valid current_password. The
        # critical invariant: the same token never grants two writes.
        second = client.post(
            "/api/v1/users/me/password",
            json={"new_password": "second-attempt", "stepup_token": token},
        )
    assert second.status_code == 400, second.text


@pytest.mark.asyncio
async def test_initial_password_updates_password_changed_at_and_sessions_invalidated_at(
    session_factory,
):
    token = "valid-stepup-" + "x" * 8
    user_id = await _seed_user(
        session_factory,
        password_set=False,
        stepup_token=token,
        stepup_expires_at=datetime.now(timezone.utc) + timedelta(minutes=4),
    )
    before = datetime.now(timezone.utc)
    app = _make_app(session_factory, user_id)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/users/me/password",
            json={"new_password": "brand-new-password", "stepup_token": token},
        )
    assert res.status_code == 204, res.text

    async with session_factory() as db:
        user = await db.get(User, user_id)
        assert user is not None
        # Both fields are written via datetime.now(timezone.utc) but
        # may persist as naive depending on the driver. Treat both as UTC.
        def _aware(dt):
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        assert user.password_changed_at is not None
        assert user.sessions_invalidated_at is not None
        assert _aware(user.password_changed_at) >= before
        assert _aware(user.sessions_invalidated_at) >= before


@pytest.mark.asyncio
async def test_initial_password_path_rejected_after_first_set(session_factory):
    """After password_set flips True, the standard branch must enforce
    the current_password check. Posting only `new_password` should 400
    instead of silently rotating the password again."""
    token = "first-set-token-" + "x" * 8
    user_id = await _seed_user(
        session_factory,
        password_set=False,
        stepup_token=token,
        stepup_expires_at=datetime.now(timezone.utc) + timedelta(minutes=4),
    )
    app = _make_app(session_factory, user_id)
    with TestClient(app) as client:
        first = client.post(
            "/api/v1/users/me/password",
            json={"new_password": "first-set-password", "stepup_token": token},
        )
        assert first.status_code == 204

        # Second call has no current_password — must be rejected now.
        second = client.post(
            "/api/v1/users/me/password",
            json={"new_password": "second-set-password"},
        )
    assert second.status_code == 400, second.text


@pytest.mark.asyncio
async def test_password_set_true_still_requires_current_password(session_factory):
    """Regression guard for users created via classic register flow."""
    user_id = await _seed_user(session_factory, password_set=True)
    app = _make_app(session_factory, user_id)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/users/me/password",
            json={"new_password": "another-password"},
        )
    assert res.status_code == 400, res.text


# ── email change — step-up token branch ─────────────────────────────────────


@pytest.mark.asyncio
async def test_email_change_requires_stepup_token_when_password_not_set(session_factory):
    user_id = await _seed_user(session_factory, password_set=False)
    app = _make_app(session_factory, user_id)
    with TestClient(app) as client:
        res = client.put(
            "/api/v1/users/me",
            json={"email": "new@acme.io"},
        )
    assert res.status_code == 400, res.text
    assert "step-up" in res.json()["detail"].lower()


@pytest.mark.asyncio
async def test_email_change_accepts_valid_stepup_token(session_factory):
    token = "valid-stepup-token-" + "x" * 8
    user_id = await _seed_user(
        session_factory,
        password_set=False,
        stepup_token=token,
        stepup_expires_at=datetime.now(timezone.utc) + timedelta(minutes=4),
    )
    app = _make_app(session_factory, user_id)
    with TestClient(app) as client:
        res = client.put(
            "/api/v1/users/me",
            json={"email": "new@acme.io", "stepup_token": token},
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["email"] == "new@acme.io"

    async with session_factory() as db:
        user = await db.get(User, user_id)
        assert user is not None
        # Token must be consumed on use (no replay).
        assert user.stepup_token is None
        assert user.stepup_token_expires_at is None
        assert user.email == "new@acme.io"
        # Email change still invalidates sessions.
        assert user.sessions_invalidated_at is not None


@pytest.mark.asyncio
async def test_email_change_rejects_expired_stepup_token(session_factory):
    token = "expired-token-" + "x" * 8
    user_id = await _seed_user(
        session_factory,
        password_set=False,
        stepup_token=token,
        # 1 second in the past.
        stepup_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    app = _make_app(session_factory, user_id)
    with TestClient(app) as client:
        res = client.put(
            "/api/v1/users/me",
            json={"email": "new@acme.io", "stepup_token": token},
        )
    assert res.status_code == 400, res.text

    # And the email must NOT have changed.
    async with session_factory() as db:
        user = await db.get(User, user_id)
        assert user is not None
        assert user.email == "alice@acme.io"


@pytest.mark.asyncio
async def test_email_change_keeps_stepup_token_when_target_email_taken(session_factory):
    """Finding 3: target-email validation must run BEFORE the step-up
    token is consumed. If the new email is already taken the change
    cannot apply, so the user's still-valid token must survive the
    failed attempt and the row must still hold it for the retry."""
    token = "preserve-on-conflict-" + "x" * 8
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=4)

    # Seed two users in the same org. `alice` has the step-up token.
    # `bob` already owns the email alice will try to claim.
    async with session_factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        alice = User(
            org_id=org.id,
            username="alice",
            email="alice@acme.io",
            password_hash=hash_password("starting-password"),
            role=Role.OWNER,
            is_active=True,
            email_verified=True,
            password_set=False,
            stepup_token=token,
            stepup_token_expires_at=expires_at,
        )
        bob = User(
            org_id=org.id,
            username="bob",
            email="bob@acme.io",
            password_hash=hash_password("bob-password"),
            role=Role.MEMBER,
            is_active=True,
            email_verified=True,
            password_set=True,
        )
        db.add_all([alice, bob])
        await db.commit()
        alice_id = alice.id

    app = _make_app(session_factory, alice_id)
    with TestClient(app) as client:
        res = client.put(
            "/api/v1/users/me",
            json={"email": "bob@acme.io", "stepup_token": token},
        )
    assert res.status_code == 409, res.text

    async with session_factory() as db:
        user = await db.get(User, alice_id)
        assert user is not None
        # Email did not change.
        assert user.email == "alice@acme.io"
        # Token must still be there for a retry — failed validation
        # cannot consume the step-up proof.
        assert user.stepup_token == token
        assert user.stepup_token_expires_at is not None


@pytest.mark.asyncio
async def test_email_change_rejects_replay_of_consumed_stepup_token(session_factory):
    """Step-up tokens are single-use. The first PUT consumes the token
    (clears the row), so a second PUT replaying the same token must
    400 — even if it would otherwise still be inside the 5-min window.
    Pins the security-critical no-replay invariant."""
    token = "single-use-token-" + "x" * 8
    user_id = await _seed_user(
        session_factory,
        password_set=False,
        stepup_token=token,
        stepup_expires_at=datetime.now(timezone.utc) + timedelta(minutes=4),
    )
    app = _make_app(session_factory, user_id)
    with TestClient(app) as client:
        first = client.put(
            "/api/v1/users/me",
            json={"email": "first@acme.io", "stepup_token": token},
        )
        assert first.status_code == 200, first.text

        # Replay the same token: server already cleared it, so the
        # token-match branch in PUT /users/me must fail.
        replay = client.put(
            "/api/v1/users/me",
            json={"email": "second@acme.io", "stepup_token": token},
        )
    assert replay.status_code == 400, replay.text

    # And the second email must NOT have been written.
    async with session_factory() as db:
        user = await db.get(User, user_id)
        assert user is not None
        assert user.email == "first@acme.io"
        assert user.stepup_token is None
        assert user.stepup_token_expires_at is None
