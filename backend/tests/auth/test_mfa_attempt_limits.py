"""BE-02 / BE-26: second-factor attempts are limited per user and per MFA
challenge (not only per IP), the challenge is single-use, and a TOTP code is
accepted once.

Each request below comes from a different client address (``CLIENT_IP_HEADER``),
the shape a distributed client takes: the per-IP limits never fire, the
per-account ones do.
"""
from __future__ import annotations

import time
from itertools import count

import jwt
import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from app import rate_limit_db
from app.auth import attempts
from app.config import settings as app_settings
from app.models.audit_event import AuditEvent
from app.models.user import User
from app.security import create_mfa_challenge_token, create_mfa_email_token
from app.services.mfa_service import encrypt_secret, generate_recovery_codes, generate_totp_secret

from tests.auth.test_session_jti_sid import (  # noqa: F401  (fixtures + helpers)
    _make_app,
    _seed_user,
    reset_limiter,
    session_factory,
)

_ips = count(1)


@pytest.fixture(autouse=True)
def distinct_client_ips(monkeypatch):
    monkeypatch.setenv("CLIENT_IP_HEADER", "x-test-client-ip")


@pytest.fixture(autouse=True)
def mfa_key(monkeypatch):
    monkeypatch.setattr(app_settings, "mfa_encryption_key", Fernet.generate_key().decode())


def _headers() -> dict[str, str]:
    n = next(_ips)
    return {"x-test-client-ip": f"203.0.{n // 250}.{n % 250 + 1}"}


async def _seed_mfa_user(factory, *, with_totp: bool = False):
    codes = generate_recovery_codes()
    seed = await _seed_user(factory, mfa_enabled=True, recovery_codes_plaintext=codes)
    secret = None
    if with_totp:
        secret = generate_totp_secret()
        async with factory() as db:
            await db.execute(
                update(User).where(User.id == seed["user_id"]).values(totp_secret=encrypt_secret(secret))
            )
            await db.commit()
    return seed["user_id"], codes, secret


def _wrong_totp(secret: str) -> str:
    """A six-digit code outside the +/- 1 window."""
    totp = pyotp.TOTP(secret)
    now = time.time()
    valid = {totp.at(now + offset) for offset in (-30, 0, 30)}
    return next(c for c in ("000000", "111111", "222222", "333333") if c not in valid)


def _recovery(client, mfa_token: str, code: str):
    return client.post(
        "/api/v1/auth/mfa/recovery", json={"mfa_token": mfa_token, "code": code}, headers=_headers()
    )


def _totp(client, mfa_token: str, code: str):
    return client.post(
        "/api/v1/auth/mfa/verify", json={"mfa_token": mfa_token, "code": code}, headers=_headers()
    )


async def _audit_rows(factory, event_type: str) -> list[AuditEvent]:
    async with factory() as db:
        return list((await db.scalars(select(AuditEvent).where(AuditEvent.event_type == event_type))).all())


@pytest.mark.asyncio
async def test_a_challenge_is_single_use(session_factory) -> None:
    user_id, codes, _ = await _seed_mfa_user(session_factory)
    mfa_token = create_mfa_challenge_token(user_id)
    with TestClient(_make_app(session_factory)) as client:
        assert _recovery(client, mfa_token, codes[0]).status_code == 200
        again = _recovery(client, mfa_token, codes[1])
    assert again.status_code == 401
    assert again.json()["detail"] == "Invalid or expired MFA token"
    async with session_factory() as db:
        stored = (await db.scalar(select(User).where(User.id == user_id))).recovery_codes
    assert len(stored.split(",")) == len(codes) - 1  # the second code was not spent


@pytest.mark.asyncio
async def test_a_challenge_is_burned_after_five_wrong_codes(session_factory) -> None:
    user_id, codes, _ = await _seed_mfa_user(session_factory)
    mfa_token = create_mfa_challenge_token(user_id)
    with TestClient(_make_app(session_factory)) as client:
        for _ in range(attempts.MFA_FAILURES_PER_CHALLENGE):
            assert _recovery(client, mfa_token, "0000-0000-0000-0000").status_code == 401
        burned = _recovery(client, mfa_token, codes[0])
        assert burned.status_code == 401
        assert burned.json()["detail"] == "Too many failed verification attempts. Sign in again."
        # Signing in again gives a fresh challenge with its own budget.
        assert _recovery(client, create_mfa_challenge_token(user_id), codes[0]).status_code == 200


@pytest.mark.asyncio
async def test_the_user_budget_spans_challenges_and_is_audited_once(session_factory) -> None:
    user_id, codes, _ = await _seed_mfa_user(session_factory)
    with TestClient(_make_app(session_factory)) as client:
        for _ in range(attempts.MFA_FAILURES_PER_USER // attempts.MFA_FAILURES_PER_CHALLENGE):
            mfa_token = create_mfa_challenge_token(user_id)
            for _ in range(attempts.MFA_FAILURES_PER_CHALLENGE):
                assert _recovery(client, mfa_token, "0000-0000-0000-0000").status_code == 401
        # Even the right code on a fresh challenge waits for the window.
        locked = _recovery(client, create_mfa_challenge_token(user_id), codes[0])
        assert locked.status_code == 429
        assert locked.json()["detail"] == "Too many failed verification attempts. Try again later."
        assert 0 < int(locked.headers["Retry-After"]) <= attempts.FAILURE_WINDOW_SECONDS
        # The email fallback is held back too.
        email = client.post(
            "/api/v1/auth/mfa/email-code",
            json={"mfa_token": create_mfa_challenge_token(user_id)},
            headers=_headers(),
        )
        assert email.status_code == 429
    rows = await _audit_rows(session_factory, "auth.mfa.locked")
    assert len(rows) == 1
    assert rows[0].actor_user_id == user_id
    assert rows[0].outcome == "failure"
    assert rows[0].detail == {
        "failures": attempts.MFA_FAILURES_PER_USER,
        "window_seconds": attempts.FAILURE_WINDOW_SECONDS,
    }


@pytest.mark.asyncio
async def test_the_window_ends_and_nothing_locks_for_good(session_factory, monkeypatch) -> None:
    user_id, codes, _ = await _seed_mfa_user(session_factory)
    with TestClient(_make_app(session_factory)) as client:
        for _ in range(attempts.MFA_FAILURES_PER_USER // attempts.MFA_FAILURES_PER_CHALLENGE):
            mfa_token = create_mfa_challenge_token(user_id)
            for _ in range(attempts.MFA_FAILURES_PER_CHALLENGE):
                _recovery(client, mfa_token, "0000-0000-0000-0000")
        assert _recovery(client, create_mfa_challenge_token(user_id), codes[0]).status_code == 429
        later = time.time() + attempts.FAILURE_WINDOW_SECONDS + 1
        monkeypatch.setattr(rate_limit_db, "_clock", lambda: later)
        assert _recovery(client, create_mfa_challenge_token(user_id), codes[0]).status_code == 200


@pytest.mark.asyncio
async def test_a_success_empties_the_user_window(session_factory) -> None:
    user_id, codes, _ = await _seed_mfa_user(session_factory)
    with TestClient(_make_app(session_factory)) as client:
        mfa_token = create_mfa_challenge_token(user_id)
        for _ in range(attempts.MFA_FAILURES_PER_CHALLENGE - 1):
            _recovery(client, mfa_token, "0000-0000-0000-0000")
        assert _recovery(client, mfa_token, codes[0]).status_code == 200
    assert rate_limit_db.get(attempts._mfa_user_key(user_id)) == 0


@pytest.mark.asyncio
async def test_wrong_email_codes_count_too(session_factory) -> None:
    user_id, _, _ = await _seed_mfa_user(session_factory)
    mfa_token = create_mfa_challenge_token(user_id)
    email_token, _ = create_mfa_email_token(user_id, "123456")
    with TestClient(_make_app(session_factory)) as client:
        for _ in range(attempts.MFA_FAILURES_PER_CHALLENGE):
            res = client.post(
                "/api/v1/auth/mfa/email-verify",
                json={"mfa_token": mfa_token, "email_token": email_token, "code": "000000"},
                headers=_headers(),
            )
            assert res.status_code == 401
        right = client.post(
            "/api/v1/auth/mfa/email-verify",
            json={"mfa_token": mfa_token, "email_token": email_token, "code": "123456"},
            headers=_headers(),
        )
    assert right.status_code == 401
    assert right.json()["detail"] == "Too many failed verification attempts. Sign in again."


@pytest.mark.asyncio
async def test_a_challenge_without_jti_is_refused(session_factory) -> None:
    user_id, codes, _ = await _seed_mfa_user(session_factory)
    legacy = jwt.encode(
        {"sub": str(user_id), "type": "mfa_challenge", "exp": int(time.time()) + 300},
        app_settings.jwt_secret_key,
        algorithm=app_settings.jwt_algorithm,
    )
    with TestClient(_make_app(session_factory)) as client:
        res = _recovery(client, legacy, codes[0])
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid or expired MFA token"


@pytest.mark.asyncio
async def test_the_store_being_down_is_a_503_not_a_pass(session_factory, monkeypatch) -> None:
    user_id, codes, _ = await _seed_mfa_user(session_factory)

    def down(*_a, **_k):
        raise OperationalError("SELECT", {}, Exception("down"))

    monkeypatch.setattr(rate_limit_db, "get", down)
    with TestClient(_make_app(session_factory)) as client:
        res = _recovery(client, create_mfa_challenge_token(user_id), codes[0])
    assert res.status_code == 503


# ── BE-26: a TOTP code is accepted once per user ────────────────────────────


@pytest.mark.asyncio
async def test_a_totp_code_cannot_be_replayed(session_factory) -> None:
    user_id, _, secret = await _seed_mfa_user(session_factory, with_totp=True)
    code = pyotp.TOTP(secret).now()
    with TestClient(_make_app(session_factory)) as client:
        assert _totp(client, create_mfa_challenge_token(user_id), code).status_code == 200
        replay = _totp(client, create_mfa_challenge_token(user_id), code)
    assert replay.status_code == 401
    assert replay.json()["detail"] == "Invalid TOTP code"


@pytest.mark.asyncio
async def test_an_older_code_is_refused_after_a_newer_one(session_factory) -> None:
    user_id, _, secret = await _seed_mfa_user(session_factory, with_totp=True)
    totp = pyotp.TOTP(secret)
    now = time.time()
    newer, older = totp.at(now + 30), totp.at(now)
    with TestClient(_make_app(session_factory)) as client:
        assert _totp(client, create_mfa_challenge_token(user_id), newer).status_code == 200
        res = _totp(client, create_mfa_challenge_token(user_id), older)
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_a_wrong_totp_code_counts_against_the_challenge(session_factory) -> None:
    user_id, _, secret = await _seed_mfa_user(session_factory, with_totp=True)
    mfa_token = create_mfa_challenge_token(user_id)
    with TestClient(_make_app(session_factory)) as client:
        for _ in range(attempts.MFA_FAILURES_PER_CHALLENGE):
            assert _totp(client, mfa_token, _wrong_totp(secret)).status_code == 401
        res = _totp(client, mfa_token, pyotp.TOTP(secret).now())
    assert res.status_code == 401
    assert res.json()["detail"] == "Too many failed verification attempts. Sign in again."
