"""BE-25: a login for a missing account does the same bcrypt work as a wrong
password, so the response time does not reveal whether the account exists."""
from __future__ import annotations

import bcrypt
import pytest
from fastapi.testclient import TestClient

from app import security
from app.security import hash_password, verify_password_or_dummy

from tests.auth.test_session_jti_sid import (  # noqa: F401  (fixtures + helpers)
    _make_app,
    _seed_user,
    reset_limiter,
    session_factory,
)


@pytest.fixture
def checkpw_calls(monkeypatch) -> list[bytes]:
    """Every hash ``bcrypt.checkpw`` is asked to verify against."""
    calls: list[bytes] = []
    real = bcrypt.checkpw

    def spy(password: bytes, hashed: bytes) -> bool:
        calls.append(hashed)
        return real(password, hashed)

    monkeypatch.setattr(security.bcrypt, "checkpw", spy)
    return calls


@pytest.mark.asyncio
async def test_missing_account_runs_one_bcrypt_check(session_factory, checkpw_calls) -> None:
    await _seed_user(session_factory)
    with TestClient(_make_app(session_factory)) as client:
        missing = client.post(
            "/api/v1/auth/login", json={"login": "nobody", "password": "whatever-1"}
        )
        wrong = client.post(
            "/api/v1/auth/login", json={"login": "alice", "password": "whatever-1"}
        )
    assert missing.status_code == wrong.status_code == 401
    assert missing.json() == wrong.json() == {"detail": "Invalid credentials"}
    # One bcrypt verification per attempt, whether or not the account exists.
    assert len(checkpw_calls) == 2


def test_dummy_hash_has_the_current_cost_and_never_matches(monkeypatch) -> None:
    monkeypatch.setattr(security, "_dummy_password_hash", None)
    assert verify_password_or_dummy("anything", None) is False
    dummy = security._dummy_password_hash
    assert dummy is not None
    # Same "$2b$<cost>$" prefix as a password hashed now.
    assert dummy[:7] == hash_password("x")[:7]
    # Made once per process, then reused.
    verify_password_or_dummy("anything", None)
    assert security._dummy_password_hash is dummy


def test_a_stored_hash_is_verified_normally() -> None:
    stored = hash_password("right-password")
    assert verify_password_or_dummy("right-password", stored) is True
    assert verify_password_or_dummy("wrong-password", stored) is False
