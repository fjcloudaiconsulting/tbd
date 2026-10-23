"""BE-32: MFA cannot be removed or bypassed with the password alone.

* Disabling MFA and regenerating recovery codes take the password AND a
  current TOTP code or an unused recovery code.
* Enrolling (``/mfa/setup``) re-authenticates: the password, or a Google
  step-up for an account without one (the step-up grid covers that proof).
* Changing the email of an MFA account takes the second factor too.
* Wrong codes and passwords on these routes draw from the per-account budgets.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.auth import attempts
from app.config import settings as app_settings
from app.models.user import User
from app.routers import users as users_module
from app.security import create_access_token
from app.services import notification_service
from app.services.mfa_service import (
    encrypt_secret,
    generate_recovery_codes,
    generate_totp_secret,
    hash_recovery_code,
)

from tests.auth.test_credential_change_ends_sessions import _app
from tests.auth.test_session_jti_sid import (  # noqa: F401  (fixtures + helpers)
    PASSWORD,
    _seed_user,
    reset_limiter,
    session_factory,
)


@pytest.fixture(autouse=True)
def mfa_key(monkeypatch):
    monkeypatch.setattr(app_settings, "mfa_encryption_key", Fernet.generate_key().decode())


@pytest.fixture(autouse=True)
def no_mail(monkeypatch):
    monkeypatch.setattr(notification_service, "send_notification_email", AsyncMock(return_value=None))

    async def _no_verification_mail(*_a, **_k):
        return None

    monkeypatch.setattr(users_module, "send_verification_email", _no_verification_mail)


async def _mfa_user(factory, *, mfa_enabled: bool = True):
    codes = generate_recovery_codes()
    seed = await _seed_user(factory, mfa_enabled=mfa_enabled, recovery_codes_plaintext=codes)
    secret = generate_totp_secret()
    async with factory() as db:
        await db.execute(
            update(User).where(User.id == seed["user_id"]).values(totp_secret=encrypt_secret(secret))
        )
        await db.commit()
        user = await db.scalar(select(User).where(User.id == seed["user_id"]))
    auth = {"Authorization": f"Bearer {create_access_token(user.id, user.org_id, user.role.value)}"}
    return user.id, codes, secret, auth


def _wrong_totp(secret: str) -> str:
    totp = pyotp.TOTP(secret)
    now = time.time()
    valid = {totp.at(now + offset) for offset in (-30, 0, 30)}
    return next(c for c in ("000000", "111111", "222222", "333333") if c not in valid)


async def _row(factory, user_id: int) -> User:
    async with factory() as db:
        return await db.scalar(select(User).where(User.id == user_id))


# ── disable ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disable_needs_a_second_factor(session_factory) -> None:
    user_id, _, secret, auth = await _mfa_user(session_factory)
    with TestClient(_app(session_factory)) as client:
        missing = client.post("/api/v1/auth/mfa/disable", json={"password": PASSWORD}, headers=auth)
        wrong = client.post(
            "/api/v1/auth/mfa/disable",
            json={"password": PASSWORD, "mfa_code": _wrong_totp(secret)},
            headers=auth,
        )
    assert missing.status_code == 422
    assert wrong.status_code == 403
    assert wrong.json()["detail"] == "Invalid authentication code"
    assert (await _row(session_factory, user_id)).mfa_enabled is True


@pytest.mark.asyncio
async def test_disable_with_a_current_totp_code(session_factory) -> None:
    user_id, _, secret, auth = await _mfa_user(session_factory)
    with TestClient(_app(session_factory)) as client:
        res = client.post(
            "/api/v1/auth/mfa/disable",
            json={"password": PASSWORD, "mfa_code": pyotp.TOTP(secret).now()},
            headers=auth,
        )
    assert res.status_code == 200, res.text
    assert (await _row(session_factory, user_id)).mfa_enabled is False


@pytest.mark.asyncio
async def test_disable_with_a_recovery_code(session_factory) -> None:
    user_id, codes, _, auth = await _mfa_user(session_factory)
    with TestClient(_app(session_factory)) as client:
        res = client.post(
            "/api/v1/auth/mfa/disable",
            json={"password": PASSWORD, "mfa_code": codes[3].upper()},
            headers=auth,
        )
    assert res.status_code == 200, res.text
    assert (await _row(session_factory, user_id)).mfa_enabled is False


@pytest.mark.asyncio
async def test_a_right_code_with_a_wrong_password_is_still_refused(session_factory) -> None:
    user_id, _, secret, auth = await _mfa_user(session_factory)
    with TestClient(_app(session_factory)) as client:
        res = client.post(
            "/api/v1/auth/mfa/disable",
            json={"password": "not-the-password", "mfa_code": pyotp.TOTP(secret).now()},
            headers=auth,
        )
    assert res.status_code == 403
    assert res.json()["detail"] == "Invalid password"
    assert (await _row(session_factory, user_id)).mfa_enabled is True


# ── regenerate ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_regenerate_needs_a_second_factor(session_factory) -> None:
    user_id, codes, secret, auth = await _mfa_user(session_factory)
    before = (await _row(session_factory, user_id)).recovery_codes
    with TestClient(_app(session_factory)) as client:
        wrong = client.post(
            "/api/v1/auth/mfa/recovery-codes",
            json={"password": PASSWORD, "mfa_code": "0000-0000-0000-0000"},
            headers=auth,
        )
        assert wrong.status_code == 403
        assert (await _row(session_factory, user_id)).recovery_codes == before

        ok = client.post(
            "/api/v1/auth/mfa/recovery-codes",
            json={"password": PASSWORD, "mfa_code": codes[0]},
            headers=auth,
        )
    assert ok.status_code == 200, ok.text
    new_codes = ok.json()["recovery_codes"]
    stored = (await _row(session_factory, user_id)).recovery_codes.split(",")
    assert stored == [hash_recovery_code(c) for c in new_codes]


# ── budgets ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wrong_codes_draw_from_the_second_factor_budget(session_factory) -> None:
    _, _, secret, auth = await _mfa_user(session_factory)
    with TestClient(_app(session_factory)) as client:
        for _ in range(attempts.MFA_FAILURES_PER_USER):
            res = client.post(
                "/api/v1/auth/mfa/recovery-codes",
                json={"password": PASSWORD, "mfa_code": "0000-0000-0000-0000"},
                headers=auth,
            )
            assert res.status_code == 403
        locked = client.post(
            "/api/v1/auth/mfa/disable",
            json={"password": PASSWORD, "mfa_code": pyotp.TOTP(secret).now()},
            headers=auth,
        )
    assert locked.status_code == 429
    assert locked.headers["Retry-After"]


@pytest.mark.asyncio
async def test_wrong_passwords_draw_from_the_login_budget(session_factory) -> None:
    _, _, secret, auth = await _mfa_user(session_factory)
    with TestClient(_app(session_factory)) as client:
        for _ in range(attempts.LOGIN_FAILURES_PER_ACCOUNT):
            res = client.post(
                "/api/v1/auth/mfa/disable",
                json={"password": "guess", "mfa_code": pyotp.TOTP(secret).now()},
                headers=auth,
            )
            assert res.status_code == 403
        assert client.post(
            "/api/v1/auth/mfa/disable",
            json={"password": PASSWORD, "mfa_code": pyotp.TOTP(secret).now()},
            headers=auth,
        ).status_code == 429
        assert client.post(
            "/api/v1/auth/login", json={"login": "alice", "password": PASSWORD}
        ).status_code == 429


# ── enrolment re-authenticates ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_setup_needs_the_password(session_factory) -> None:
    user_id, _, _, auth = await _mfa_user(session_factory, mfa_enabled=False)
    async with session_factory() as db:
        await db.execute(update(User).where(User.id == user_id).values(totp_secret=None))
        await db.commit()
    with TestClient(_app(session_factory)) as client:
        missing = client.post("/api/v1/auth/mfa/setup", json={}, headers=auth)
        wrong = client.post("/api/v1/auth/mfa/setup", json={"current_password": "guess"}, headers=auth)
        assert (await _row(session_factory, user_id)).totp_secret is None
        ok = client.post("/api/v1/auth/mfa/setup", json={"current_password": PASSWORD}, headers=auth)
    assert missing.status_code == wrong.status_code == 400
    assert wrong.json()["detail"] == "Current password is incorrect"
    assert ok.status_code == 200, ok.text
    assert ok.json()["secret"]
    assert (await _row(session_factory, user_id)).totp_secret is not None


@pytest.mark.asyncio
async def test_setup_for_an_sso_account_needs_a_google_step_up(session_factory) -> None:
    user_id, _, _, auth = await _mfa_user(session_factory, mfa_enabled=False)
    async with session_factory() as db:
        await db.execute(update(User).where(User.id == user_id).values(password_set=False, totp_secret=None))
        await db.commit()
    with TestClient(_app(session_factory)) as client:
        res = client.post("/api/v1/auth/mfa/setup", json={"current_password": PASSWORD}, headers=auth)
    assert res.status_code == 400
    assert res.json()["detail"] == (
        "Step-up verification with Google is required to set up two-factor authentication"
    )
    assert (await _row(session_factory, user_id)).totp_secret is None


# ── email change on an MFA account ──────────────────────────────────────────

EMAIL_DETAIL = (
    "An authenticator or recovery code is required to change the email of an account with two-factor on"
)


@pytest.mark.asyncio
async def test_email_change_on_an_mfa_account_needs_the_second_factor(session_factory) -> None:
    user_id, _, secret, auth = await _mfa_user(session_factory)
    body = {"email": "new@example.com", "current_password": PASSWORD}
    with TestClient(_app(session_factory)) as client:
        missing = client.put("/api/v1/users/me", json=body, headers=auth)
        wrong = client.put("/api/v1/users/me", json={**body, "mfa_code": _wrong_totp(secret)}, headers=auth)
        assert (await _row(session_factory, user_id)).pending_email is None
        ok = client.put(
            "/api/v1/users/me", json={**body, "mfa_code": pyotp.TOTP(secret).now()}, headers=auth
        )
    assert missing.status_code == wrong.status_code == 400
    assert missing.json()["detail"] == wrong.json()["detail"] == EMAIL_DETAIL
    assert ok.status_code == 200, ok.text
    assert (await _row(session_factory, user_id)).pending_email == "new@example.com"


@pytest.mark.asyncio
async def test_a_taken_address_does_not_spend_the_recovery_code(session_factory) -> None:
    user_id, codes, _, auth = await _mfa_user(session_factory)
    async with session_factory() as db:
        owner = await db.scalar(select(User).where(User.id == user_id))
        db.add(User(
            org_id=owner.org_id, username="bob", email="taken@example.com",
            password_hash=owner.password_hash, role=owner.role, is_superadmin=False,
            is_active=True, email_verified=True,
        ))
        await db.commit()
    before = (await _row(session_factory, user_id)).recovery_codes
    with TestClient(_app(session_factory)) as client:
        res = client.put(
            "/api/v1/users/me",
            json={"email": "taken@example.com", "current_password": PASSWORD, "mfa_code": codes[0]},
            headers=auth,
        )
    assert res.status_code == 409
    assert (await _row(session_factory, user_id)).recovery_codes == before


@pytest.mark.asyncio
async def test_email_change_without_mfa_is_unchanged(session_factory) -> None:
    user_id, _, _, auth = await _mfa_user(session_factory, mfa_enabled=False)
    with TestClient(_app(session_factory)) as client:
        res = client.put(
            "/api/v1/users/me",
            json={"email": "new@example.com", "current_password": PASSWORD},
            headers=auth,
        )
    assert res.status_code == 200, res.text
    assert (await _row(session_factory, user_id)).pending_email == "new@example.com"
