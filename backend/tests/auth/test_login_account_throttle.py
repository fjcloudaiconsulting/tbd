"""BE-02: failed passwords are limited per account beside the per-IP limit.

Every request comes from a different client address, so the per-IP
``10/minute`` never fires and only the per-account budget can stop it.
"""
from __future__ import annotations

from itertools import count

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import attempts
from app.models.audit_event import AuditEvent
from app.security import create_password_reset_token

from tests.auth.test_session_jti_sid import (  # noqa: F401  (fixtures + helpers)
    PASSWORD,
    _make_app,
    _seed_user,
    reset_limiter,
    session_factory,
)

_ips = count(1)


@pytest.fixture(autouse=True)
def distinct_client_ips(monkeypatch):
    monkeypatch.setenv("CLIENT_IP_HEADER", "x-test-client-ip")


def _login(client, login: str, password: str):
    n = next(_ips)
    return client.post(
        "/api/v1/auth/login",
        json={"login": login, "password": password},
        headers={"x-test-client-ip": f"198.51.{n // 250}.{n % 250 + 1}"},
    )


def _fill(client, login: str) -> None:
    for _ in range(attempts.LOGIN_FAILURES_PER_ACCOUNT):
        assert _login(client, login, "wrong-password").status_code == 401


async def _locked_rows(factory) -> list[AuditEvent]:
    async with factory() as db:
        return list(
            (await db.scalars(select(AuditEvent).where(AuditEvent.event_type == "auth.login.locked"))).all()
        )


@pytest.mark.asyncio
async def test_a_full_window_refuses_even_the_right_password(session_factory) -> None:
    seed = await _seed_user(session_factory)
    with TestClient(_make_app(session_factory)) as client:
        _fill(client, "alice")
        res = _login(client, "alice", PASSWORD)
        # What was typed is normalised, so case does not open a new budget.
        upper = _login(client, "ALICE", PASSWORD)
    assert res.status_code == upper.status_code == 429
    assert res.json()["detail"] == "Too many failed sign-in attempts. Try again later."
    assert 0 < int(res.headers["Retry-After"]) <= attempts.FAILURE_WINDOW_SECONDS
    rows = await _locked_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].actor_user_id == seed["user_id"]
    assert rows[0].detail == {
        "failures": attempts.LOGIN_FAILURES_PER_ACCOUNT,
        "window_seconds": attempts.FAILURE_WINDOW_SECONDS,
    }


@pytest.mark.asyncio
async def test_an_unknown_login_is_limited_the_same_way_and_not_audited(session_factory) -> None:
    await _seed_user(session_factory)
    with TestClient(_make_app(session_factory)) as client:
        _fill(client, "nobody")
        res = _login(client, "nobody", PASSWORD)
        other = _login(client, "alice", PASSWORD)
    assert res.status_code == 429
    assert res.json()["detail"] == "Too many failed sign-in attempts. Try again later."
    assert other.status_code == 200  # another account's budget is untouched
    assert await _locked_rows(session_factory) == []


@pytest.mark.asyncio
async def test_a_success_empties_the_window(session_factory) -> None:
    await _seed_user(session_factory)
    with TestClient(_make_app(session_factory)) as client:
        for _ in range(attempts.LOGIN_FAILURES_PER_ACCOUNT - 1):
            _login(client, "alice", "wrong-password")
        assert _login(client, "alice", PASSWORD).status_code == 200
        for _ in range(attempts.LOGIN_FAILURES_PER_ACCOUNT - 1):
            _login(client, "alice", "wrong-password")
        assert _login(client, "alice", PASSWORD).status_code == 200


@pytest.mark.asyncio
async def test_a_password_reset_reopens_the_window(session_factory) -> None:
    seed = await _seed_user(session_factory)
    with TestClient(_make_app(session_factory)) as client:
        _fill(client, "alice")
        _fill(client, "alice@example.com")
        assert _login(client, "alice", PASSWORD).status_code == 429
        reset = client.post(
            "/api/v1/auth/reset-password",
            json={"token": create_password_reset_token(seed["user_id"]), "new_password": "a-new-passw0rd"},
        )
        assert reset.status_code == 200, reset.text
        assert _login(client, "alice", "a-new-passw0rd").status_code == 200
        assert _login(client, "alice@example.com", "a-new-passw0rd").status_code == 200
