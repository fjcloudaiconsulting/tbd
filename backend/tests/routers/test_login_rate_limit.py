"""Login limits and outage behaviour; rate limits move to MySQL (INFRA-121)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import RedisError, ResponseError

from app import redis_client
from app.routers import auth as auth_router
from tests.routers.test_auth import (  # noqa: F401  (fixtures + helpers)
    _make_app,
    _seed_user,
    reset_limiter,
    session_factory,
)


class _DeadClient:
    def __init__(self, exc: Exception):
        self._exc = exc

    async def ping(self):
        return True

    async def set(self, *a, **k):
        raise self._exc

    async def get(self, *a, **k):
        raise self._exc


_MODES = {
    "no_client": lambda: None,
    "redis_error": lambda: _DeadClient(RedisError("down")),
    "closed_transport": lambda: _DeadClient(RuntimeError("Transport is closed")),
    "oom_on_set": lambda: _DeadClient(
        ResponseError("OOM command not allowed when used memory > 'maxmemory'")
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", list(_MODES))
async def test_l1_login_is_uniform_while_valkey_is_down(session_factory, monkeypatch, mode):
    await _seed_user(session_factory, username="alice", email="alice@acme.io", password="pw-alice-1")
    await _seed_user(
        session_factory, username="bob", email="bob@acme.io", password="pw-bob-1", mfa_enabled=True
    )
    monkeypatch.setattr(redis_client, "get_client", _MODES[mode])
    attempts = [
        ("alice", "pw-alice-1"),
        ("alice", "wrong"),
        ("bob", "pw-bob-1"),
        ("bob", "wrong"),
        ("nobody", "whatever"),
    ]
    with TestClient(_make_app(session_factory), raise_server_exceptions=False) as client:
        seen = {
            (r.status_code, r.text)
            for r in (
                client.post("/api/v1/auth/login", json={"login": u, "password": p})
                for u, p in attempts
            )
        }
    assert len(seen) == 1, seen
    assert next(iter(seen))[0] == 503


@pytest.mark.asyncio
async def test_l2_attempts_are_counted_while_valkey_is_down(session_factory, monkeypatch):
    await _seed_user(session_factory)
    monkeypatch.setattr(redis_client, "get_client", lambda: None)
    with TestClient(_make_app(session_factory)) as client:
        codes = [
            client.post("/api/v1/auth/login", json={"login": "alice", "password": "x"}).status_code
            for _ in range(11)
        ]
    assert codes == [503] * 10 + [429]


@pytest.mark.asyncio
async def test_l3_failed_logins_are_counted_with_valkey_up(session_factory):
    await _seed_user(session_factory, password="right-password-1")
    with TestClient(_make_app(session_factory)) as client:
        for _ in range(10):
            r = client.post("/api/v1/auth/login", json={"login": "alice", "password": "wrong"})
            assert r.status_code == 401
        r = client.post(
            "/api/v1/auth/login", json={"login": "alice", "password": "right-password-1"}
        )
    assert r.status_code == 429


@pytest.mark.asyncio
async def test_l4_db_down_fails_the_request_before_the_handler(
    session_factory, limits_db_down, monkeypatch
):
    await _seed_user(session_factory, password="right-password-1")
    ran = []
    monkeypatch.setattr(auth_router, "verify_password", lambda *a: ran.append(1) or True)
    with TestClient(_make_app(session_factory), raise_server_exceptions=False) as client:
        r = client.post(
            "/api/v1/auth/login", json={"login": "alice", "password": "right-password-1"}
        )
    assert r.status_code == 500
    assert ran == []
