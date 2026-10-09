"""The MFA email token is single-use via a used_tokens claim (INFRA-122):
issuing writes nothing, a wrong code does not burn it, the first right code
claims its jti, a replay is a 401."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import state_db
from app.security import create_mfa_challenge_token, create_mfa_email_token

from tests.auth.test_session_jti_sid import (  # noqa: F401  (fixtures + helpers)
    _make_app,
    _seed_user,
    reset_limiter,
    session_factory,
)


def _claims() -> int:
    with state_db._engine.connect() as c:
        return c.execute(
            select(func.count()).select_from(state_db._U).where(state_db._U.c.scope == "mfa_email")
        ).scalar()


@pytest.mark.asyncio
async def test_issue_writes_nothing(session_factory, monkeypatch) -> None:
    seed = await _seed_user(session_factory, mfa_enabled=True)
    mfa_token = create_mfa_challenge_token(seed["user_id"])
    from app.routers import auth as auth_module

    async def _no_mail(*a, **k):
        return None

    monkeypatch.setattr(auth_module, "send_mfa_email_code", _no_mail)
    with TestClient(_make_app(session_factory)) as client:
        res = client.post("/api/v1/auth/mfa/email-code", json={"mfa_token": mfa_token})
    assert res.status_code == 200, res.text
    assert res.json()["email_token"]
    assert _claims() == 0


@pytest.mark.asyncio
async def test_verify_claims_the_jti_and_a_replay_is_401(session_factory) -> None:
    seed = await _seed_user(session_factory, mfa_enabled=True)
    mfa_token = create_mfa_challenge_token(seed["user_id"])
    email_token, _jti = create_mfa_email_token(seed["user_id"], "123456")
    body = {"mfa_token": mfa_token, "email_token": email_token}
    with TestClient(_make_app(session_factory)) as client:
        wrong = client.post("/api/v1/auth/mfa/email-verify", json={**body, "code": "654321"})
        assert wrong.status_code == 401
        assert _claims() == 0  # a typo does not burn the token

        first = client.post("/api/v1/auth/mfa/email-verify", json={**body, "code": "123456"})
        assert first.status_code == 200, first.text
        assert _claims() == 1

        replay = client.post("/api/v1/auth/mfa/email-verify", json={**body, "code": "123456"})
    assert replay.status_code == 401
    assert replay.json()["detail"] == "Invalid or expired email code"
