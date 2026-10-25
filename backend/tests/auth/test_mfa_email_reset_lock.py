"""BE-03: no email-code sign-in for 72 hours after an emailed password reset.

A reset through the emailed link is proven by the mailbox alone, so the
sign-in after it must take TOTP or a recovery code: the email fallback is
refused for 72 hours from the reset even when the user opted in, and works
again after. An in-app password change (session plus current password) does
not open the window.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pyotp
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.models.user import User
from app.routers import auth as auth_module
from app.security import (
    create_mfa_challenge_token,
    create_password_reset_token,
    wait_out_cutoff,
)

from tests.auth.test_credential_change_ends_sessions import NEW_PASSWORD, _app
from tests.auth.test_mfa_email_fallback import _email_code, _email_verify
from tests.auth.test_mfa_settings_second_factor import (  # noqa: F401  (fixtures + helpers)
    _mfa_user,
    _row,
    mfa_key,
    no_mail,
)
from tests.auth.test_session_jti_sid import (  # noqa: F401  (fixtures + helpers)
    PASSWORD,
    reset_limiter,
    session_factory,
)

PAUSED = auth_module.EMAIL_FALLBACK_PAUSED_DETAIL


@pytest.fixture
def mailer(monkeypatch) -> AsyncMock:
    sent = AsyncMock(return_value=None)
    monkeypatch.setattr(auth_module, "send_mfa_email_code", sent)
    return sent


async def _opted_in(factory):
    user_id, codes, secret, auth = await _mfa_user(factory)
    async with factory() as db:
        await db.execute(update(User).where(User.id == user_id).values(mfa_email_fallback=True))
        await db.commit()
    return user_id, codes, secret, auth


async def _reset_at(factory, user_id: int, when: datetime) -> None:
    async with factory() as db:
        await db.execute(
            update(User)
            .where(User.id == user_id)
            .values(password_reset_at=when.astimezone(timezone.utc).replace(tzinfo=None))
        )
        await db.commit()


@pytest.mark.asyncio
async def test_an_emailed_reset_refuses_the_email_code_but_not_totp(session_factory, mailer) -> None:
    user_id, codes, secret, _ = await _opted_in(session_factory)
    with TestClient(_app(session_factory)) as client:
        reset = client.post(
            "/api/v1/auth/reset-password",
            json={"token": create_password_reset_token(user_id, "alice@example.com"), "new_password": NEW_PASSWORD},
        )
        assert reset.status_code == 200, reset.text
        row = await _row(session_factory, user_id)
        assert row.password_reset_at is not None
        assert row.mfa_email_fallback is True  # the opt-in survives; the window overrides it
        await wait_out_cutoff(row)  # a challenge minted inside the reset's second is refused

        sent = _email_code(client, user_id)
        signed_in_by_email = _email_verify(client, user_id)
        totp = client.post(
            "/api/v1/auth/mfa/verify",
            json={"mfa_token": create_mfa_challenge_token(user_id), "code": pyotp.TOTP(secret).now()},
        )
        recovery = client.post(
            "/api/v1/auth/mfa/recovery",
            json={"mfa_token": create_mfa_challenge_token(user_id), "code": codes[0]},
        )
    assert sent.status_code == 400
    assert sent.json()["detail"] == PAUSED
    mailer.assert_not_called()
    assert signed_in_by_email.status_code == 400
    assert signed_in_by_email.json()["detail"] == PAUSED
    assert totp.status_code == 200, totp.text
    assert recovery.status_code == 200, recovery.text


@pytest.mark.asyncio
async def test_the_email_code_works_again_after_72_hours(session_factory, mailer) -> None:
    user_id, _, _, _ = await _opted_in(session_factory)
    now = datetime.now(timezone.utc)
    with TestClient(_app(session_factory)) as client:
        await _reset_at(session_factory, user_id, now - timedelta(hours=71, minutes=59))
        inside = _email_code(client, user_id)
        await _reset_at(session_factory, user_id, now - timedelta(hours=72, minutes=1))
        after = _email_code(client, user_id)
        signed_in = _email_verify(client, user_id)
    assert inside.status_code == 400
    assert inside.json()["detail"] == PAUSED
    assert after.status_code == 200, after.text
    assert signed_in.status_code == 200, signed_in.text
    mailer.assert_called_once()


@pytest.mark.asyncio
async def test_the_window_is_72_hours_from_the_reset(session_factory) -> None:
    user_id, _, _, _ = await _opted_in(session_factory)
    reset_at = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
    await _reset_at(session_factory, user_id, reset_at)
    user = await _row(session_factory, user_id)

    with pytest.raises(HTTPException) as refused:
        auth_module._require_email_fallback(user, now=reset_at + timedelta(hours=72, seconds=-1))
    assert refused.value.detail == PAUSED
    auth_module._require_email_fallback(user, now=reset_at + timedelta(hours=72))


@pytest.mark.asyncio
async def test_off_wins_over_the_window_message(session_factory) -> None:
    user_id, _, _, _ = await _mfa_user(session_factory)
    await _reset_at(session_factory, user_id, datetime.now(timezone.utc))
    with TestClient(_app(session_factory)) as client:
        sent = _email_code(client, user_id)
    assert sent.status_code == 400
    assert sent.json()["detail"] == auth_module.EMAIL_FALLBACK_OFF_DETAIL


@pytest.mark.asyncio
async def test_an_in_app_password_change_opens_no_window(session_factory, mailer) -> None:
    user_id, _, _, auth = await _opted_in(session_factory)
    with TestClient(_app(session_factory)) as client:
        changed = client.post(
            "/api/v1/users/me/password",
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            headers=auth,
        )
        assert changed.status_code == 204, changed.text
        row = await _row(session_factory, user_id)
        assert row.password_reset_at is None
        await wait_out_cutoff(row)
        sent = _email_code(client, user_id)
    assert sent.status_code == 200, sent.text
    mailer.assert_called_once()
