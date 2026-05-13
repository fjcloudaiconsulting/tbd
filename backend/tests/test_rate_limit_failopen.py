"""Regression tests for slowapi fail-open behaviour on Redis storage errors.

Prod 2026-05-13 incident: ``redis.exceptions.TimeoutError`` raised by the
underlying ``limits.storage.RedisStorage.incr`` (EVALSHA) propagated up
through ``slowapi.extension._check_request_limit`` and surfaced as HTTP 500
on every rate-limited auth endpoint (reset-password, login, etc.).

This module pins:

1. ``RateLimitExceeded`` continues to raise 429 (legitimate limits preserved).
2. Redis-storage errors (``TimeoutError`` / ``ConnectionError`` / generic
   ``RedisError``) on ``.incr()`` are caught and the protected endpoint
   returns its normal response (NOT 500).
3. A structured ``rate_limit.degraded`` warning is emitted exactly once per
   failed request with ``error_type``, ``path``, and ``backend="redis"``.
4. The wrapper does not interfere when Redis is healthy.
"""
from __future__ import annotations

import json
from typing import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError, TimeoutError as RedisTimeoutError
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.extension import _rate_limit_exceeded_handler

from app import rate_limit
from app.rate_limit_failopen import FailOpenRedisStorage, wrap_limiter_failopen


# ── Unit: storage-level fail-open ──────────────────────────────────────────


class _ExplodingRedisStorage:
    """Stand-in for ``limits.storage.RedisStorage`` that raises the given
    exception from every read/write method. Used to feed ``FailOpenRedisStorage``
    a deterministic failure source without standing up a broken Redis.
    """

    def __init__(self, exc: Exception):
        self._exc = exc
        self.calls: list[str] = []

    def incr(self, key, expiry, amount=1):
        self.calls.append("incr")
        raise self._exc

    def get(self, key):
        self.calls.append("get")
        raise self._exc

    def get_expiry(self, key):
        self.calls.append("get_expiry")
        raise self._exc

    def clear(self, key):
        self.calls.append("clear")
        raise self._exc

    def check(self):
        self.calls.append("check")
        raise self._exc

    def reset(self):
        self.calls.append("reset")
        raise self._exc


@pytest.mark.parametrize(
    "exc",
    [
        RedisTimeoutError("Timeout reading from socket"),
        RedisConnectionError("Connection refused"),
    ],
    ids=["timeout", "connection"],
)
def test_failopen_storage_incr_returns_zero_on_redis_error(exc):
    """``incr`` is the write path. Returning ``0`` means slowapi sees
    "current count is 0 <= limit", so the request is permitted (fail open).
    """
    wrapped = FailOpenRedisStorage.__new__(FailOpenRedisStorage)
    wrapped._inner = _ExplodingRedisStorage(exc)

    result = wrapped.incr("k", 60, amount=1)

    assert result == 0, "fail-open incr must return 0 to permit the request"


@pytest.mark.parametrize(
    "exc",
    [RedisTimeoutError("t"), RedisConnectionError("c"), OSError("os")],
)
def test_failopen_storage_get_returns_zero_on_storage_error(exc):
    wrapped = FailOpenRedisStorage.__new__(FailOpenRedisStorage)
    wrapped._inner = _ExplodingRedisStorage(exc)
    assert wrapped.get("k") == 0


def test_failopen_storage_get_expiry_returns_none_on_error():
    wrapped = FailOpenRedisStorage.__new__(FailOpenRedisStorage)
    wrapped._inner = _ExplodingRedisStorage(RedisTimeoutError("t"))
    # ``get_expiry`` returning a low number is the safest default — slowapi
    # only uses this for header injection. Return ``time.time()`` so headers
    # encode "no remaining wait" rather than corrupting the response.
    assert wrapped.get_expiry("k") is not None


def test_failopen_storage_passes_through_when_inner_succeeds():
    """When the inner storage returns a value, the wrapper passes it
    through verbatim. No fail-open path taken when Redis is healthy.
    """

    class _GoodStorage:
        def incr(self, key, expiry, amount=1):
            return 7

        def get(self, key):
            return 3

        def get_expiry(self, key):
            return 1234.5

    wrapped = FailOpenRedisStorage.__new__(FailOpenRedisStorage)
    wrapped._inner = _GoodStorage()
    assert wrapped.incr("k", 60) == 7
    assert wrapped.get("k") == 3
    assert wrapped.get_expiry("k") == 1234.5


# ── Integration: endpoint stays 200 + structured warning fires ─────────────


def _build_app_with_exploding_limiter(exc: Exception) -> FastAPI:
    """Build a tiny FastAPI app whose Limiter's storage raises ``exc`` on
    ``incr``. Mirrors the production wiring path: the wrapper sits below
    slowapi so ``_check_request_limit`` never sees the underlying error.
    """
    limiter = Limiter(
        key_func=lambda: "test-client",
        storage_uri="memory://",  # placeholder; we overwrite _storage next
    )
    # Force storage creation so we can inspect/replace it.
    _ = limiter._storage
    wrap_limiter_failopen(limiter)
    # Swap the inner storage for one that always raises.
    limiter._storage._inner = _ExplodingRedisStorage(exc)

    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    @app.post("/probe")
    @limiter.limit("5/minute")
    async def probe(request: Request):
        return {"ok": True}

    return app


@pytest.mark.parametrize(
    "exc",
    [
        RedisTimeoutError("Timeout reading from socket"),
        RedisConnectionError("Connection refused"),
    ],
    ids=["timeout", "connection"],
)
def test_endpoint_returns_200_when_redis_storage_raises(exc):
    """Acceptance criterion 1+3: rate-limited endpoint returns its normal
    200 response when the underlying Redis storage raises
    ``TimeoutError`` / ``ConnectionError`` on ``.incr()`` — NOT 500.
    """
    app = _build_app_with_exploding_limiter(exc)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/probe")
    assert resp.status_code == 200, (
        f"expected fail-open 200, got {resp.status_code}: {resp.text}"
    )
    assert resp.json() == {"ok": True}


def test_degraded_log_event_emitted_with_required_fields(capsys):
    """Acceptance criterion 2: a structured ``rate_limit.degraded`` warning
    surfaces with ``error_type``, ``path`` (query stripped), and
    ``backend="redis"``. Emitted exactly once per failed request.
    """
    app = _build_app_with_exploding_limiter(RedisTimeoutError("Timeout"))
    with TestClient(app, raise_server_exceptions=False) as client:
        # Include a query string to confirm we strip it from the logged path.
        resp = client.post("/probe?secret=abc")
    assert resp.status_code == 200

    captured = capsys.readouterr()
    out = captured.out + captured.err
    # structlog renders JSON when configured by the app; in test context it
    # may render key=value. Tolerate both shapes — assert on the substrings.
    assert "rate_limit.degraded" in out, (
        "expected a rate_limit.degraded log event"
    )
    assert "error_type" in out and "TimeoutError" in out
    assert 'backend' in out and 'redis' in out
    assert "/probe" in out
    assert "secret=abc" not in out, (
        "query string must be stripped from logged path"
    )
    # Exactly one degraded event per request (no retry spam).
    assert out.count("rate_limit.degraded") == 1


def test_rate_limit_exceeded_still_raises_429():
    """Acceptance criterion 1 (the OTHER half): ``RateLimitExceeded`` is
    NOT swallowed by the fail-open wrapper. Legitimate 429s preserved.

    We use a healthy in-memory storage wrapped by the fail-open layer and
    hammer the endpoint past its limit. The wrapper passes through, the
    counter increments past the limit, and slowapi raises 429.
    """
    limiter = Limiter(
        key_func=lambda: "rate-test-client",
        storage_uri="memory://",
    )
    _ = limiter._storage
    wrap_limiter_failopen(limiter)
    # Leave the healthy MemoryStorage as inner — no exception path.

    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    @app.get("/tight")
    @limiter.limit("2/minute")
    async def tight(request: Request):
        return {"ok": True}

    with TestClient(app) as client:
        first = client.get("/tight")
        second = client.get("/tight")
        third = client.get("/tight")

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429, (
        f"expected legitimate 429 on third call, got {third.status_code}"
    )
