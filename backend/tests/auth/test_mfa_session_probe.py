"""INFRA-132: the second-factor endpoints probe the session store before the
code check, so a right and a wrong code get the same status and body while it
is down or full, and a recovery code is not consumed."""
from __future__ import annotations

import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings as app_settings
from app.models.user import User
from app.routers.auth import SESSION_REDIS_UNAVAILABLE_DETAIL
from app.security import create_mfa_challenge_token, create_mfa_email_token
from app.services.mfa_service import (
    encrypt_secret,
    generate_recovery_codes,
    generate_totp_secret,
)

from tests.auth.test_session_jti_sid import (  # noqa: F401  (fixtures + helpers)
    _canonical_refresh_cookie,
    _make_app,
    _seed_user,
    reset_limiter,
    session_factory,
)

@pytest.mark.asyncio
async def test_mfa_endpoints_answer_alike_when_session_store_fails(
    session_factory, monkeypatch, state_db_down
) -> None:
    monkeypatch.setattr(
        app_settings, "mfa_encryption_key", Fernet.generate_key().decode()
    )
    codes = generate_recovery_codes(count=3)
    seed = await _seed_user(
        session_factory, mfa_enabled=True, recovery_codes_plaintext=codes
    )
    secret = generate_totp_secret()
    async with session_factory() as db:
        user = await db.scalar(select(User).where(User.id == seed["user_id"]))
        user.totp_secret = encrypt_secret(secret)
        await db.commit()
        before_codes = user.recovery_codes

    mfa_token = create_mfa_challenge_token(seed["user_id"])
    email_token, _ = create_mfa_email_token(seed["user_id"], "123456")

    cases = {
        "/api/v1/auth/mfa/verify": (
            {"code": pyotp.TOTP(secret).now()},
            {"code": "000000"},
        ),
        "/api/v1/auth/mfa/recovery": ({"code": codes[0]}, {"code": "nope-nope"}),
        "/api/v1/auth/mfa/email-verify": (
            {"code": "123456", "email_token": email_token},
            {"code": "654321", "email_token": email_token},
        ),
    }
    app = _make_app(session_factory)
    with TestClient(app) as client:
        for path, (right, wrong) in cases.items():
            seen = []
            # An unknown mfa_token answers alike too: kills probing only after
            # the token resolves.
            for token, extra in ((mfa_token, right), (mfa_token, wrong), ("garbage", right)):
                res = client.post(path, json={"mfa_token": token, **extra})
                assert _canonical_refresh_cookie(res.headers) is None
                seen.append((res.status_code, res.json()))
            assert seen[0] == seen[1] == seen[2] == (
                503,
                {"detail": SESSION_REDIS_UNAVAILABLE_DETAIL},
            ), (path, seen)

    async with session_factory() as db:
        user = await db.scalar(select(User).where(User.id == seed["user_id"]))
        assert user.recovery_codes == before_codes
