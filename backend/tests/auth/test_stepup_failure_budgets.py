"""TBD-716 / TBD-735: the step-ups that mint tokens use the per-user budgets.

The PAT, agent-token and OAuth-consent mints share ``_verify_step_up``:

* a wrong ``current_password`` draws from the account's password budget (the
  one ``/login`` and the settings re-auth gates use), and a full budget is a
  429 even with the right password;
* the TOTP ``mfa_code`` is accepted once per user (the replay guard of
  ``/mfa/verify``), and wrong codes draw from the second-factor budget;
* a recovery code is still not accepted here.

The password change and the email-change password check draw from the same
password budget.
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
from app.routers import agent_tokens as agent_tokens_module
from app.routers import oauth as oauth_module
from app.routers import users as users_module
from app.routers.api_tokens import _verify_step_up
from app.services import notification_service
from app.services.mfa_service import encrypt_secret, generate_recovery_codes, hash_recovery_code

from tests.auth.test_credential_change_ends_sessions import _app
from tests.auth.test_session_jti_sid import (  # noqa: F401  (fixtures + helpers)
    PASSWORD as USER_PASSWORD,
    _seed_user,
    reset_limiter,
    session_factory,
)
from tests.routers.test_api_tokens import (  # noqa: F401  (fixtures + helpers)
    BASE,
    PASSWORD,
    _jwt_for,
    _jwt_h,
    _make_client,
    _reset_limiter,
    _seed_superadmin,
    factory,
)


WRONG_PASSWORD = "wrong"
NEW_PASSWORD = "new-password"


@pytest.fixture(autouse=True)
def mfa_key(monkeypatch):
    monkeypatch.setattr(app_settings, "mfa_encryption_key", Fernet.generate_key().decode())


@pytest.fixture(autouse=True)
def no_mail(monkeypatch):
    monkeypatch.setattr(notification_service, "send_notification_email", AsyncMock(return_value=None))

    async def _no_verification_mail(*_a, **_k):
        return None

    monkeypatch.setattr(users_module, "send_verification_email", _no_verification_mail)


def _mint(client: TestClient, jwt: str, **proof) -> int:
    body = {"name": "cron", "scope": "write", "expires_in_days": 30, **proof}
    return client.post(BASE, json=body, headers=_jwt_h(jwt)).status_code


def _wrong_totp(secret: str) -> str:
    totp = pyotp.TOTP(secret)
    now = time.time()
    valid = {totp.at(now + offset) for offset in (-30, 0, 30)}
    return next(c for c in ("000000", "111111", "222222", "333333") if c not in valid)


async def _mfa_superadmin(factory) -> tuple[int, str, str]:
    secret = pyotp.random_base32()
    uid = await _seed_superadmin(
        factory, password_set=True, mfa_enabled=True, mfa_secret=encrypt_secret(secret)
    )
    return uid, secret, await _jwt_for(factory, uid)


# ── every token-minting step-up goes through the one shared check ───────────


def test_every_token_mint_uses_the_shared_step_up() -> None:
    assert agent_tokens_module._verify_step_up is _verify_step_up
    assert oauth_module._verify_step_up is _verify_step_up


# ── TOTP replay (TBD-716) ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_totp_code_mints_once(factory) -> None:
    _, secret, jwt = await _mfa_superadmin(factory)
    code = pyotp.TOTP(secret).now()
    with _make_client(factory) as client:
        first = _mint(client, jwt, current_password=PASSWORD, mfa_code=code)
        replay = _mint(client, jwt, current_password=PASSWORD, mfa_code=code)
    assert first == 201
    assert replay == 401


@pytest.mark.asyncio
async def test_wrong_codes_draw_from_the_second_factor_budget(factory) -> None:
    # The mint route allows 10 calls an hour, so the budget is pre-filled to
    # one short and the route supplies the failure that fills it.
    uid, secret, jwt = await _mfa_superadmin(factory)
    for _ in range(attempts.MFA_FAILURES_PER_USER - 1):
        await attempts.record_mfa_failure(uid, None)
    assert await attempts.mfa_user_retry_after(uid) is None
    with _make_client(factory) as client:
        assert _mint(client, jwt, current_password=PASSWORD, mfa_code=_wrong_totp(secret)) == 401
        assert await attempts.mfa_user_retry_after(uid) is not None
        # Full budget: even a current code is refused until the window ends.
        locked = client.post(
            BASE,
            json={
                "name": "cron", "scope": "write", "expires_in_days": 30,
                "current_password": PASSWORD, "mfa_code": pyotp.TOTP(secret).now(),
            },
            headers=_jwt_h(jwt),
        )
    assert locked.status_code == 429
    assert "Retry-After" in locked.headers


@pytest.mark.asyncio
async def test_a_recovery_code_does_not_mint(factory) -> None:
    uid, _, jwt = await _mfa_superadmin(factory)
    codes = generate_recovery_codes()
    async with factory() as s:
        await s.execute(
            update(User)
            .where(User.id == uid)
            .values(recovery_codes=",".join(hash_recovery_code(c) for c in codes))
        )
        await s.commit()
    with _make_client(factory) as client:
        assert _mint(client, jwt, current_password=PASSWORD, mfa_code=codes[0]) == 401
    async with factory() as s:
        stored = (await s.scalar(select(User.recovery_codes).where(User.id == uid))).split(",")
    assert len(stored) == len(codes)  # nothing spent


# ── password budget (TBD-735) ───────────────────────────────────────────────


async def _fill_password_budget_but_one(user_id: int) -> None:
    for _ in range(attempts.LOGIN_FAILURES_PER_ACCOUNT - 1):
        await attempts.record_password_failure(None, user_id)
    assert await attempts.password_retry_after(user_id) is None


@pytest.mark.asyncio
async def test_wrong_mint_passwords_draw_from_the_password_budget(factory) -> None:
    uid = await _seed_superadmin(factory, password_set=True)
    jwt = await _jwt_for(factory, uid)
    await _fill_password_budget_but_one(uid)
    with _make_client(factory) as client:
        assert _mint(client, jwt, current_password=WRONG_PASSWORD) == 401
        # The same budget /login checks, keyed on the user id.
        assert await attempts.password_retry_after(uid) is not None
        assert _mint(client, jwt, current_password=PASSWORD) == 429


async def _signed_in(factory) -> tuple[int, dict]:
    from app.security import create_access_token

    seed = await _seed_user(factory)
    async with factory() as db:
        user = await db.scalar(select(User).where(User.id == seed["user_id"]))
    return user.id, {
        "Authorization": f"Bearer {create_access_token(user.id, user.org_id, user.role.value)}"
    }


@pytest.mark.asyncio
async def test_wrong_current_passwords_at_password_change_draw_from_the_budget(
    session_factory,
) -> None:
    user_id, auth = await _signed_in(session_factory)
    await _fill_password_budget_but_one(user_id)
    body = {"current_password": WRONG_PASSWORD, "new_password": NEW_PASSWORD}
    with TestClient(_app(session_factory)) as client:
        res = client.post("/api/v1/users/me/password", json=body, headers=auth)
        assert res.status_code == 400, res.text
        assert res.json()["detail"] == "Current password is incorrect"
        locked = client.post(
            "/api/v1/users/me/password",
            json={**body, "current_password": USER_PASSWORD},
            headers=auth,
        )
    assert locked.status_code == 429
    assert await attempts.password_retry_after(user_id) is not None


@pytest.mark.asyncio
async def test_wrong_passwords_at_email_change_draw_from_the_budget(session_factory) -> None:
    user_id, auth = await _signed_in(session_factory)
    await _fill_password_budget_but_one(user_id)
    body = {"email": "moved@example.com", "current_password": WRONG_PASSWORD}
    with TestClient(_app(session_factory)) as client:
        res = client.put("/api/v1/users/me", json=body, headers=auth)
        assert res.status_code == 400, res.text
        assert res.json()["detail"] == (
            "Current password is required and must be correct to change email"
        )
        locked = client.put(
            "/api/v1/users/me", json={**body, "current_password": USER_PASSWORD}, headers=auth
        )
    assert locked.status_code == 429
    assert await attempts.password_retry_after(user_id) is not None


# ── /verify-email password check (TBD-657 gate) ─────────────────────────────


@pytest.mark.asyncio
async def test_wrong_passwords_at_verify_email_draw_from_the_budget(session_factory) -> None:
    from app.security import create_email_verification_token
    from tests.auth.test_verify_email_endpoint import EMAIL, make_app
    from tests.auth.test_verify_email_endpoint import PASSWORD as VERIFY_PASSWORD
    from tests.auth.test_verify_email_endpoint import _seed_user as _seed_unverified

    user_id = await _seed_unverified(session_factory, email_verified=False)
    token = create_email_verification_token(user_id, EMAIL)
    await _fill_password_budget_but_one(user_id)
    with TestClient(make_app(session_factory)) as client:
        # Asking without a password is how the page learns one is needed:
        # it is not a failed attempt.
        ask = client.post("/api/v1/auth/verify-email", json={"token": token})
        assert ask.json()["detail"]["code"] == "password_required"
        assert await attempts.password_retry_after(user_id) is None

        wrong = client.post(
            "/api/v1/auth/verify-email", json={"token": token, "password": WRONG_PASSWORD}
        )
        assert wrong.status_code == 400
        assert wrong.json()["detail"]["code"] == "password_incorrect"
        locked = client.post(
            "/api/v1/auth/verify-email", json={"token": token, "password": VERIFY_PASSWORD}
        )
    assert locked.status_code == 429
    async with session_factory() as db:
        assert (await db.scalar(select(User.email_verified).where(User.id == user_id))) is False
