"""Fail-open wrapper for slowapi's Redis-backed rate-limit storage.

Production incident 2026-05-13: ``redis.exceptions.TimeoutError`` raised by
the underlying ``limits.storage.RedisStorage.incr`` (EVALSHA) propagated
up through ``slowapi.extension._check_request_limit`` and surfaced as
HTTP 500 on every rate-limited auth endpoint (forgot-password, login,
register, refresh, etc.). slowapi only swallows storage errors if
``in_memory_fallback_enabled=True`` is passed to ``Limiter``, and even
then it only falls back the SECOND time the dead-storage flag is checked.
The first request after Redis hiccups still 500s.

This module installs a thin wrapper around the limits-library storage
object so the failure never reaches slowapi. The protected endpoint
serves its normal response (200 / 401 / etc.) when Redis is unreachable.
A structured ``rate_limit.degraded`` warning is emitted once per failed
request so the outage is visible in logs.

Trade-off (intentional): rate limiting is BYPASSED during a Redis outage.
This is the correct bias for production auth availability — a brief
Redis blip should not lock everyone out of the password-reset flow. The
window is small (seconds, bounded by Redis recovery) and the
``rate_limit.degraded`` event surfaces in DO logs so the incident is
investigable. Healthy-state behaviour is unchanged.

Shape: a forwarding proxy that exposes the same surface as
``limits.storage.RedisStorage`` (the methods slowapi's ``RateLimiter``
strategies call: ``incr``, ``get``, ``get_expiry``, ``clear``, ``check``,
``reset``). Each method catches the storage-error family and returns a
safe default. Non-storage methods (eg. limits-library internals) are
forwarded with ``__getattr__`` so subclass / isinstance checks against
``MovingWindowSupport`` etc. still work via ``_inner``'s MRO.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Any

import structlog
from redis.exceptions import RedisError
from slowapi import Limiter
from starlette.requests import Request

logger = structlog.stdlib.get_logger()


# Set by ``Limiter._check_request_limit`` (wrapped below) so the storage
# layer can recover the request path for the ``rate_limit.degraded``
# log without slowapi having to pass the request down through the limits
# library API. Reset back to None in the same wrapper after the call.
_current_request_cv: ContextVar[Request | None] = ContextVar(
    "_rate_limit_failopen_request", default=None
)


# The set of exception types we treat as "storage degraded — fail open".
# ``RedisError`` covers the redis-py family (TimeoutError, ConnectionError,
# ResponseError, etc.). ``ConnectionError``/``TimeoutError`` are the
# builtin ones surfaced by the socket layer when redis-py is misbehaving.
# ``OSError`` catches the lowest-level socket failures (eg. ECONNREFUSED
# when Redis is fully down).
_STORAGE_ERRORS: tuple[type[BaseException], ...] = (
    RedisError,
    ConnectionError,
    TimeoutError,
    OSError,
)


def _request_path(request: Request | None) -> str:
    """Return ``request.url.path`` with query string stripped, or empty
    string when no request is bound. Query strings can carry
    password-reset tokens, MFA codes, etc. — never log them.
    """
    if request is None:
        return ""
    try:
        return request.url.path
    except Exception:  # pragma: no cover — defensive
        return ""


def _current_request() -> Request | None:
    """Look up the in-flight request bound by the wrapped
    ``Limiter._check_request_limit`` (see ``wrap_limiter_failopen``).
    """
    return _current_request_cv.get()


def _log_degraded(error: BaseException, method: str) -> None:
    """Emit the ``rate_limit.degraded`` structured warning.

    Called from the storage method that detected the Redis failure.
    Only the write-path method (``incr``) calls this — the read-path
    methods (``get``, ``get_expiry``) silently fail open without
    re-logging, since they are downstream of the same failed request
    and would otherwise produce 2-3 duplicate log lines per request as
    slowapi computes counters + injects rate-limit response headers.
    This gives exactly one ``rate_limit.degraded`` event per request
    by construction — no contextvar bookkeeping required.
    """
    request = _current_request()
    logger.warning(
        "rate_limit.degraded",
        error_type=type(error).__name__,
        error_message=str(error),
        path=_request_path(request),
        method=method,
        backend="redis",
    )


class FailOpenRedisStorage:
    """Forwarding proxy over a ``limits.storage.RedisStorage`` instance
    that catches storage-layer exceptions and returns safe defaults.

    Safe defaults are chosen so that, from slowapi's perspective, the
    limit is NOT exceeded:

    - ``incr`` returns ``0``. slowapi compares ``incr_result <= item.amount``;
      ``0 <= anything`` permits the request.
    - ``get`` returns ``0`` (same reasoning, used by the ``test`` path).
    - ``get_expiry`` returns the current epoch — header injection then
      encodes "no remaining wait" rather than a corrupt value.
    - ``clear`` / ``reset`` are no-ops on failure.
    - ``check`` returns ``True`` (storage is "ok enough to keep using").

    Subclass / isinstance checks against ``limits.storage.base`` mixins
    (``MovingWindowSupport``, ``SlidingWindowCounterSupport``) are
    forwarded via ``__getattr__``, so slowapi's strategy selection still
    sees the underlying storage's capabilities.
    """

    __slots__ = ("_inner",)

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    # ── Fixed-window strategy hot paths ────────────────────────────────

    def incr(self, key: str, expiry: int, amount: int = 1) -> int:
        try:
            return self._inner.incr(key, expiry, amount=amount)
        except _STORAGE_ERRORS as exc:
            _log_degraded(exc, "incr")
            return 0

    # Read-path methods silently fail open: they run downstream of
    # ``incr`` within the SAME request after ``incr`` already logged
    # and returned 0. Re-logging here would produce 2-3 duplicate
    # ``rate_limit.degraded`` lines per failed request as slowapi
    # computes window stats and injects response headers.

    def get(self, key: str) -> int:
        try:
            return self._inner.get(key)
        except _STORAGE_ERRORS:
            return 0

    def get_expiry(self, key: str) -> float:
        try:
            return self._inner.get_expiry(key)
        except _STORAGE_ERRORS:
            # Current epoch so header injection encodes "no remaining
            # wait" instead of corrupt data.
            return time.time()

    def clear(self, key: str) -> None:
        try:
            self._inner.clear(key)
        except _STORAGE_ERRORS:
            pass

    def check(self) -> bool:
        try:
            return bool(self._inner.check())
        except _STORAGE_ERRORS:
            return True

    def reset(self) -> None:
        # ``reset()`` is called from test fixtures (``limiter.reset()``)
        # and is best-effort in production. Swallow storage errors so
        # tests against a dead Redis do not crash.
        try:
            self._inner.reset()
        except _STORAGE_ERRORS:
            pass

    # ── Moving-window strategy support ─────────────────────────────────

    def acquire_entry(
        self, key: str, limit: int, expiry: int, amount: int = 1
    ) -> bool:
        try:
            return bool(
                self._inner.acquire_entry(key, limit, expiry, amount=amount)
            )
        except _STORAGE_ERRORS as exc:
            _log_degraded(exc, "acquire_entry")
            return True  # permit the request

    def get_moving_window(
        self, key: str, limit: int, expiry: int
    ) -> tuple[int, int]:
        try:
            return self._inner.get_moving_window(key, limit, expiry)
        except _STORAGE_ERRORS:
            return (int(time.time()), 0)

    # ── Sliding-window-counter support ─────────────────────────────────

    def acquire_sliding_window_entry(
        self, key: str, limit: int, expiry: int, amount: int = 1
    ) -> bool:
        try:
            return bool(
                self._inner.acquire_sliding_window_entry(
                    key, limit, expiry, amount=amount
                )
            )
        except _STORAGE_ERRORS as exc:
            _log_degraded(exc, "acquire_sliding_window_entry")
            return True

    def get_sliding_window(
        self, key: str, expiry: int
    ) -> tuple[int, float, int, float]:
        try:
            return self._inner.get_sliding_window(key, expiry)
        except _STORAGE_ERRORS:
            return (0, 0.0, 0, 0.0)

    # ── Forward everything else ────────────────────────────────────────

    def __getattr__(self, name: str) -> Any:
        # __getattr__ is only called when normal lookup fails (ie. for
        # attributes we didn't override). Forward to the inner storage.
        return getattr(self._inner, name)


def wrap_limiter_failopen(limiter: Limiter) -> Limiter:
    """Replace ``limiter._storage`` with a ``FailOpenRedisStorage`` wrapper
    and rebuild the underlying ``RateLimiter`` strategy against it. Also
    wrap ``Limiter._check_request_limit`` so the in-flight ``Request`` is
    available to the storage layer (for ``rate_limit.degraded`` log
    context) without slowapi having to pass it down.

    Call this once after constructing the ``Limiter`` (at module import
    time in ``rate_limit.py``). The wrapper is applied unconditionally —
    even for in-memory storage in local dev, the layer is a no-op cost
    when the inner storage does not raise.
    """
    # Touch ``_storage`` to force slowapi's lazy storage init.
    inner = limiter._storage
    if isinstance(inner, FailOpenRedisStorage):  # idempotent
        return limiter

    wrapped = FailOpenRedisStorage(inner)
    limiter._storage = wrapped

    # The strategy (FixedWindow / MovingWindow / SlidingWindow) holds a
    # reference to the original storage in ``self.storage``. Repoint it
    # so all hot-path calls go through the wrapper.
    if hasattr(limiter, "_limiter") and limiter._limiter is not None:
        limiter._limiter.storage = wrapped

    # Wrap ``_check_request_limit`` so we can bind the current request to
    # a contextvar for log context. slowapi's storage API does not pass
    # the request down to ``incr`` / ``hit``, so without this we have no
    # way to recover the path / method for the degraded warning.
    original_check = limiter._check_request_limit

    def check_with_request_bound(
        request: Request,
        endpoint_func: Any,
        in_middleware: bool = True,
    ) -> None:
        token = _current_request_cv.set(request)
        try:
            return original_check(request, endpoint_func, in_middleware)
        finally:
            _current_request_cv.reset(token)

    limiter._check_request_limit = check_with_request_bound  # type: ignore[assignment]

    return limiter
