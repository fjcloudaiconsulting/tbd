"""BE-03: the email-code MFA fallback is opt-in for an account with TOTP.

* Off by default: ``/mfa/email-code`` mails nothing and ``/mfa/email-verify``
  issues no session, so the mailbox alone cannot stand in for TOTP.
* ``PUT /mfa/email-fallback`` turns it on with a current TOTP code or an
  unused recovery code (it lowers protection, as the BE-32 changes do), and
  off with the session alone. Disabling MFA turns it off too.
* An MFA account with no TOTP secret keeps the email code: there it would be
  the only code-based factor. ``mfa_enable`` needs a TOTP code, so the app
  cannot create such an account; the check still never strands one.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.models.audit_event import AuditEvent
from app.models.notification import Notification
from app.models.user import User
from app.routers import auth as auth_module
from app.security import create_mfa_challenge_token, create_mfa_email_token

from tests.auth.test_credential_change_ends_sessions import _app
from tests.auth.test_mfa_settings_second_factor import (  # noqa: F401  (fixtures + helpers)
    _mfa_user,
    _row,
    _wrong_totp,
    mfa_key,
    no_mail,
)
from tests.auth.test_session_jti_sid import (  # noqa: F401  (fixtures + helpers)
    PASSWORD,
    _seed_user,
    reset_limiter,
    session_factory,
)

FALLBACK = "/api/v1/auth/mfa/email-fallback"


@pytest.fixture
def mailer(monkeypatch) -> AsyncMock:
    sent = AsyncMock(return_value=None)
    monkeypatch.setattr(auth_module, "send_mfa_email_code", sent)
    return sent


def _email_code(client, user_id: int):
    return client.post(
        "/api/v1/auth/mfa/email-code",
        json={"mfa_token": create_mfa_challenge_token(user_id)},
    )


def _email_verify(client, user_id: int):
    email_token, _jti = create_mfa_email_token(user_id, "123456")
    return client.post(
        "/api/v1/auth/mfa/email-verify",
        json={
            "mfa_token": create_mfa_challenge_token(user_id),
            "email_token": email_token,
            "code": "123456",
        },
    )


async def _audit(factory, event_type: str) -> list[AuditEvent]:
    async with factory() as db:
        return list(
            (await db.execute(select(AuditEvent).where(AuditEvent.event_type == event_type)))
            .scalars()
            .all()
        )


# ── off by default ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_totp_account_starts_with_the_fallback_off(session_factory, mailer) -> None:
    user_id, _, _, auth = await _mfa_user(session_factory)
    assert (await _row(session_factory, user_id)).mfa_email_fallback is False
    with TestClient(_app(session_factory)) as client:
        me = client.get("/api/v1/auth/me", headers=auth)
        sent = _email_code(client, user_id)
    assert me.status_code == 200, me.text
    assert me.json()["mfa_email_fallback"] is False
    assert sent.status_code == 400
    assert sent.json()["detail"] == auth_module.EMAIL_FALLBACK_OFF_DETAIL
    assert "email_token" not in sent.json()
    mailer.assert_not_called()


@pytest.mark.asyncio
async def test_an_email_code_cannot_complete_sign_in_while_off(session_factory) -> None:
    user_id, _, _, _ = await _mfa_user(session_factory)
    with TestClient(_app(session_factory)) as client:
        res = _email_verify(client, user_id)
    assert res.status_code == 400
    assert res.json()["detail"] == auth_module.EMAIL_FALLBACK_OFF_DETAIL
    assert "access_token" not in res.json()
    assert "refresh_token" not in res.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_an_mfa_account_without_totp_keeps_the_email_code(session_factory, mailer) -> None:
    seed = await _seed_user(session_factory, mfa_enabled=True)
    with TestClient(_app(session_factory)) as client:
        sent = _email_code(client, seed["user_id"])
        signed_in = _email_verify(client, seed["user_id"])
    assert sent.status_code == 200, sent.text
    assert signed_in.status_code == 200, signed_in.text
    mailer.assert_called_once()


# ── turning it on ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_turning_it_on_needs_a_second_factor(session_factory) -> None:
    user_id, _, secret, auth = await _mfa_user(session_factory)
    with TestClient(_app(session_factory)) as client:
        missing = client.put(FALLBACK, json={"enabled": True}, headers=auth)
        wrong = client.put(
            FALLBACK, json={"enabled": True, "mfa_code": _wrong_totp(secret)}, headers=auth
        )
    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert wrong.json()["detail"] == "Invalid authentication code"
    assert (await _row(session_factory, user_id)).mfa_email_fallback is False
    assert await _audit(session_factory, "user.mfa.email_fallback.enabled") == []


@pytest.mark.asyncio
async def test_on_with_a_totp_code_lets_the_email_code_sign_in(session_factory, mailer) -> None:
    user_id, _, secret, auth = await _mfa_user(session_factory)
    with TestClient(_app(session_factory)) as client:
        res = client.put(
            FALLBACK, json={"enabled": True, "mfa_code": pyotp.TOTP(secret).now()}, headers=auth
        )
        me = client.get("/api/v1/auth/me", headers=auth)
        sent = _email_code(client, user_id)
        signed_in = _email_verify(client, user_id)
    assert res.status_code == 200, res.text
    assert res.json() == {"mfa_email_fallback": True}
    assert me.json()["mfa_email_fallback"] is True
    assert sent.status_code == 200, sent.text
    assert sent.json()["email_token"]
    mailer.assert_called_once()
    assert signed_in.status_code == 200, signed_in.text
    assert signed_in.json()["access_token"]
    (row,) = await _audit(session_factory, "user.mfa.email_fallback.enabled")
    assert row.actor_user_id == user_id
    assert row.outcome.value == "success"
    async with session_factory() as db:
        (notice,) = (
            await db.execute(
                select(Notification).where(
                    Notification.event_type == "user.mfa.email_fallback.enabled"
                )
            )
        ).scalars().all()
    assert notice.user_id == user_id
    assert notice.audit_event_id == row.id


@pytest.mark.asyncio
async def test_on_with_a_recovery_code_spends_it(session_factory) -> None:
    user_id, codes, _, auth = await _mfa_user(session_factory)
    with TestClient(_app(session_factory)) as client:
        res = client.put(FALLBACK, json={"enabled": True, "mfa_code": codes[2]}, headers=auth)
    assert res.status_code == 200, res.text
    row = await _row(session_factory, user_id)
    assert row.mfa_email_fallback is True
    assert len(row.recovery_codes.split(",")) == len(codes) - 1


@pytest.mark.asyncio
async def test_off_needs_no_code_and_closes_the_email_code(session_factory, mailer) -> None:
    user_id, _, _, auth = await _mfa_user(session_factory)
    async with session_factory() as db:
        await db.execute(update(User).where(User.id == user_id).values(mfa_email_fallback=True))
        await db.commit()
    with TestClient(_app(session_factory)) as client:
        res = client.put(FALLBACK, json={"enabled": False}, headers=auth)
        sent = _email_code(client, user_id)
    assert res.status_code == 200, res.text
    assert res.json() == {"mfa_email_fallback": False}
    assert (await _row(session_factory, user_id)).mfa_email_fallback is False
    assert sent.status_code == 400
    mailer.assert_not_called()
    assert len(await _audit(session_factory, "user.mfa.email_fallback.disabled")) == 1


@pytest.mark.asyncio
async def test_the_toggle_needs_mfa(session_factory) -> None:
    seed = await _seed_user(session_factory)
    from app.security import create_access_token

    async with session_factory() as db:
        user = await db.scalar(select(User).where(User.id == seed["user_id"]))
    auth = {"Authorization": f"Bearer {create_access_token(user.id, user.org_id, user.role.value)}"}
    with TestClient(_app(session_factory)) as client:
        res = client.put(FALLBACK, json={"enabled": False}, headers=auth)
    assert res.status_code == 400
    assert res.json()["detail"] == "MFA is not enabled"


@pytest.mark.asyncio
async def test_disabling_mfa_turns_the_fallback_off(session_factory) -> None:
    user_id, _, secret, auth = await _mfa_user(session_factory)
    async with session_factory() as db:
        await db.execute(update(User).where(User.id == user_id).values(mfa_email_fallback=True))
        await db.commit()
    with TestClient(_app(session_factory)) as client:
        res = client.post(
            "/api/v1/auth/mfa/disable",
            json={"password": PASSWORD, "mfa_code": pyotp.TOTP(secret).now()},
            headers=auth,
        )
    assert res.status_code == 200, res.text
    row = await _row(session_factory, user_id)
    assert row.mfa_enabled is False
    assert row.mfa_email_fallback is False
