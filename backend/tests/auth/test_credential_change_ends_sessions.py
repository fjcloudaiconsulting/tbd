"""BE-04: a password reset or change, and an MFA enable or disable, reliably
end the user's earlier sessions.

* Every session family of the user is deleted (MFA changes keep the caller's).
* The cutoff is the next whole second, so a token minted earlier in the same
  second as the change is refused whatever the database does with fractions.
* A sign-in in that second waits it out and gets a working session.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import security, state_db
from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_session_factory
from app.models.user import User
from app.rate_limit import limiter
from app.routers.auth import router as auth_router
from app.routers.users import router as users_router
from app.security import (
    create_mfa_challenge_token,
    create_password_reset_token,
    session_cutoff,
    token_cutoff,
    wait_out_cutoff,
)
from app.services.mfa_service import encrypt_secret, generate_recovery_codes, generate_totp_secret
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from tests.auth.test_session_jti_sid import (  # noqa: F401  (fixtures + helpers)
    PASSWORD,
    _canonical_refresh_cookie,
    _refresh_token_from_set_cookie,
    _seed_user,
    reset_limiter,
    session_factory,
)
from tests.conftest import set_refresh_cookie

NEW_PASSWORD = "a-brand-new-passw0rd"


@pytest.fixture(autouse=True)
def mfa_key(monkeypatch):
    monkeypatch.setattr(app_settings, "mfa_encryption_key", Fernet.generate_key().decode())


def _app(factory) -> FastAPI:
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    async def override_session_factory():
        return factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.include_router(auth_router)
    app.include_router(users_router)
    return app


def _sign_in(client, password: str = PASSWORD) -> tuple[str, str]:
    """(access token, refresh token) of a fresh password sign-in."""
    res = client.post("/api/v1/auth/login", json={"login": "alice", "password": password})
    assert res.status_code == 200, res.text
    return res.json()["access_token"], _refresh_token_from_set_cookie(_canonical_refresh_cookie(res.headers))


def _families(user_id: int) -> set[str]:
    with state_db._engine.connect() as c:
        return set(c.execute(select(state_db._F.c.sid).where(state_db._F.c.user_id == user_id)).scalars())


def _refresh(client, refresh_token: str):
    set_refresh_cookie(client, refresh_token)
    return client.post("/api/v1/auth/refresh")


def _sid(token: str) -> str:
    return security.decode_refresh_jti_sid(token)[1]


# ── Every family is deleted ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_password_change_deletes_every_session_family(session_factory) -> None:
    seed = await _seed_user(session_factory)
    with TestClient(_app(session_factory)) as client:
        access, _mine = _sign_in(client)
        _, other = _sign_in(client)
        assert len(_families(seed["user_id"])) == 2
        res = client.post(
            "/api/v1/users/me/password",
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            headers={"Authorization": f"Bearer {access}"},
        )
        assert res.status_code == 204, res.text
        assert _families(seed["user_id"]) == set()
        assert _refresh(client, other).status_code == 401


@pytest.mark.asyncio
async def test_password_reset_deletes_every_session_family(session_factory) -> None:
    seed = await _seed_user(session_factory)
    with TestClient(_app(session_factory)) as client:
        _, refresh_token = _sign_in(client)
        res = client.post(
            "/api/v1/auth/reset-password",
            json={"token": create_password_reset_token(seed["user_id"]), "new_password": NEW_PASSWORD},
        )
        assert res.status_code == 200, res.text
        assert _families(seed["user_id"]) == set()
        assert _refresh(client, refresh_token).status_code == 401


# ── The same-second edge ────────────────────────────────────────────────────


def test_the_cutoff_is_the_next_whole_second() -> None:
    base = datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc)
    for fraction in (0, 1, 300_000, 499_999, 500_000, 999_999):
        cutoff = session_cutoff(base.replace(microsecond=fraction))
        assert cutoff == base + timedelta(seconds=1)
        assert cutoff.microsecond == 0
        # A token minted earlier in the event's second is refused.
        assert datetime.fromtimestamp(int(base.timestamp()), tz=timezone.utc) < cutoff


@pytest.mark.asyncio
async def test_a_reset_at_a_sub_second_fraction_refuses_that_seconds_tokens(
    session_factory, monkeypatch
) -> None:
    """The reset lands at .3 of a second. The stored cutoff carries no
    fraction (nothing for MySQL to round down) and is past every token minted
    earlier in that second."""
    seed = await _seed_user(session_factory)
    event = datetime.now(timezone.utc).replace(microsecond=300_000)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return event

    monkeypatch.setattr(security, "datetime", _Frozen)
    with TestClient(_app(session_factory)) as client:
        res = client.post(
            "/api/v1/auth/reset-password",
            json={"token": create_password_reset_token(seed["user_id"]), "new_password": NEW_PASSWORD},
        )
    assert res.status_code == 200, res.text
    async with session_factory() as db:
        user = await db.scalar(select(User).where(User.id == seed["user_id"]))
    expected = event.replace(microsecond=0) + timedelta(seconds=1)
    assert user.sessions_invalidated_at.replace(tzinfo=timezone.utc) == expected
    assert user.password_changed_at.replace(tzinfo=timezone.utc) == expected
    same_second_iat = datetime.fromtimestamp(int(event.timestamp()), tz=timezone.utc)
    assert same_second_iat < token_cutoff(user)


@pytest.mark.asyncio
async def test_a_sign_in_right_after_a_change_gets_a_working_session(session_factory) -> None:
    await _seed_user(session_factory)
    with TestClient(_app(session_factory)) as client:
        access, _ = _sign_in(client)
        res = client.post(
            "/api/v1/users/me/password",
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            headers={"Authorization": f"Bearer {access}"},
        )
        assert res.status_code == 204, res.text
        # What the settings page does next: sign straight back in.
        new_access, new_refresh = _sign_in(client, NEW_PASSWORD)
        assert client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {new_access}"}
        ).status_code == 200
        assert _refresh(client, new_refresh).status_code == 200
        # And the pre-change access token is dead.
        assert client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {access}"}
        ).status_code == 401


@pytest.mark.asyncio
async def test_wait_out_cutoff_sleeps_only_inside_the_cutoff_second(monkeypatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(security.asyncio, "sleep", fake_sleep)
    now = datetime.now(timezone.utc)
    ahead = User(password_changed_at=None, sessions_invalidated_at=now + timedelta(milliseconds=600))
    await wait_out_cutoff(ahead)
    assert len(slept) == 1 and 0 < slept[0] <= 0.6
    passed = User(password_changed_at=now - timedelta(seconds=5), sessions_invalidated_at=None)
    await wait_out_cutoff(passed)
    assert len(slept) == 1


@pytest.mark.asyncio
async def test_a_challenge_from_before_a_reset_is_refused(session_factory) -> None:
    codes = generate_recovery_codes()
    seed = await _seed_user(session_factory, mfa_enabled=True, recovery_codes_plaintext=codes)
    challenge = create_mfa_challenge_token(seed["user_id"])
    with TestClient(_app(session_factory)) as client:
        reset = client.post(
            "/api/v1/auth/reset-password",
            json={"token": create_password_reset_token(seed["user_id"]), "new_password": NEW_PASSWORD},
        )
        assert reset.status_code == 200, reset.text
        res = client.post("/api/v1/auth/mfa/recovery", json={"mfa_token": challenge, "code": codes[0]})
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid or expired MFA token"


# ── MFA changes end the OTHER sessions ──────────────────────────────────────


@pytest.mark.asyncio
async def test_mfa_enable_ends_other_sessions_and_keeps_this_one(session_factory) -> None:
    seed = await _seed_user(session_factory)
    secret = generate_totp_secret()
    async with session_factory() as db:
        await db.execute(
            update(User).where(User.id == seed["user_id"]).values(totp_secret=encrypt_secret(secret))
        )
        await db.commit()
    with TestClient(_app(session_factory)) as client:
        _, other = _sign_in(client)
        access, mine = _sign_in(client)
        set_refresh_cookie(client, mine)
        res = client.post(
            "/api/v1/auth/mfa/enable",
            json={"code": pyotp.TOTP(secret).now()},
            headers={"Authorization": f"Bearer {access}"},
        )
        assert res.status_code == 200, res.text
        assert _families(seed["user_id"]) == {_sid(mine)}
        assert _refresh(client, other).status_code == 401
        assert _refresh(client, mine).status_code == 200


@pytest.mark.asyncio
async def test_a_failed_revoke_leaves_mfa_unchanged(session_factory, monkeypatch) -> None:
    seed = await _seed_user(session_factory)
    secret = generate_totp_secret()
    async with session_factory() as db:
        await db.execute(
            update(User).where(User.id == seed["user_id"]).values(totp_secret=encrypt_secret(secret))
        )
        await db.commit()

    async def down(*_a, **_k):
        from sqlalchemy.exc import OperationalError

        raise OperationalError("DELETE", {}, Exception("down"))

    with TestClient(_app(session_factory)) as client:
        access, _ = _sign_in(client)
        monkeypatch.setattr(state_db, "session_revoke_user", down)
        res = client.post(
            "/api/v1/auth/mfa/enable",
            json={"code": pyotp.TOTP(secret).now()},
            headers={"Authorization": f"Bearer {access}"},
        )
    assert res.status_code == 503
    async with session_factory() as db:
        assert await db.scalar(
            select(func.count()).select_from(User).where(User.id == seed["user_id"], User.mfa_enabled.is_(True))
        ) == 0
