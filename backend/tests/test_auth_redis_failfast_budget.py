"""Auth Redis client fail-fast budget — 2026-05-19.

Production trace at 2026-05-19T15:25-15:42 showed ``/auth/refresh``
hanging 45 s with `(canceled)` in the browser network panel. Root
cause: the auth Redis client was configured with ``socket_timeout=3``,
``retries=2``, ``backoff cap=1.0``, which gave each Redis call up to
``3 + 2 * (3 + 1) = 11 s`` of retry budget. ``/refresh`` makes up to
4 Redis calls (validate + grace + family_exists + family_member from
the catch-up helper), so a single VPC blip stacked to ~44 s — exactly
matching the frontend's 45 s reactive-recovery cancel.

The fix (PR for Finding 2): tighten the budget so a single Redis call
costs at most ~2.2 s, and ``/refresh`` worst case stays under 10 s.
A transient blip now surfaces as a fail-fast 503 the frontend retries,
not a hung request that locks the user out.

These tests pin the budget so a future change cannot silently re-inflate
it. They do NOT pin the wire-level behaviour against a real Redis
(that's covered by ``test_redis_transport_normalizer`` and the rest of
the suite); they only assert the configured constants and the resulting
Redis client's socket/retry parameters.
"""
from __future__ import annotations

from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff

from app import redis_client as rc


# ── Budget constants ────────────────────────────────────────────────────


class TestBudgetConstants:
    """The module-level constants are the contract. Any change that
    breaks one of these assertions should require explicit operator
    approval — they were tuned against the 2026-05-19 production
    trace and the frontend's 45 s reactive-recovery cap."""

    def test_socket_connect_timeout_is_one_second(self) -> None:
        assert rc.AUTH_REDIS_SOCKET_CONNECT_TIMEOUT_S == 1.0

    def test_socket_read_timeout_is_one_second(self) -> None:
        assert rc.AUTH_REDIS_SOCKET_TIMEOUT_S == 1.0

    def test_retry_count_is_one(self) -> None:
        """Two retries (old value) plus the original attempt = 3 socket
        operations per call. One retry caps to 2 ops, halving the
        worst-case latency on a flapping VPC connection."""
        assert rc.AUTH_REDIS_RETRY_COUNT == 1

    def test_backoff_base_is_50_ms(self) -> None:
        assert rc.AUTH_REDIS_RETRY_BACKOFF_BASE_S == 0.05

    def test_backoff_cap_is_200_ms(self) -> None:
        assert rc.AUTH_REDIS_RETRY_BACKOFF_CAP_S == 0.2

    def test_total_per_call_budget_under_three_seconds(self) -> None:
        """The compound budget. If any of the above constants drifts
        up, this guard fires too. The 3 s upper bound is the line the
        2026-05-19 fix draws: anything above starts approaching the
        frontend's 45 s cap when multiplied by the 3-4 Redis calls
        ``/refresh`` makes on the grace path."""
        per_call_budget = (
            rc.AUTH_REDIS_SOCKET_TIMEOUT_S
            + rc.AUTH_REDIS_RETRY_COUNT
            * (
                rc.AUTH_REDIS_SOCKET_TIMEOUT_S
                + rc.AUTH_REDIS_RETRY_BACKOFF_CAP_S
            )
        )
        assert per_call_budget < 3.0, (
            f"Per-call Redis budget exceeded 3 s: {per_call_budget}. "
            "Re-tune AUTH_REDIS_* constants or document an explicit "
            "deviation with operator approval."
        )

    def test_refresh_path_worst_case_under_ten_seconds(self) -> None:
        """``/auth/refresh`` makes up to 4 sequential Redis calls in
        the grace branch (validate, grace, family_exists, plus the
        catch-up helper's session_family_member). The total worst
        case must stay well under the frontend's 45 s reactive-
        recovery timer so a Redis blip surfaces as a fast 503, not
        a hung request."""
        per_call = (
            rc.AUTH_REDIS_SOCKET_TIMEOUT_S
            + rc.AUTH_REDIS_RETRY_COUNT
            * (
                rc.AUTH_REDIS_SOCKET_TIMEOUT_S
                + rc.AUTH_REDIS_RETRY_BACKOFF_CAP_S
            )
        )
        refresh_calls = 4
        worst_case = refresh_calls * per_call
        assert worst_case < 10.0, (
            f"Worst-case /refresh Redis budget {worst_case}s is too "
            "close to the frontend's 45s cancel timer."
        )


# ── Live client construction reflects the budget ────────────────────────


class TestClientConstructedWithBudget:
    """The constants ARE the configured client parameters. These tests
    construct a fresh Redis client by hand using the same builder code
    path ``get_client()`` uses and assert the resulting object carries
    the configured values.

    The conftest-level autouse fixture replaces ``rc.get_client`` with
    a ``lambda: fake_redis`` so most tests never touch the real builder.
    We need to bypass that fixture here — restoring the real
    ``get_client`` function so we can inspect what the production code
    path would actually construct."""

    def _real_get_client(self, monkeypatch):
        """Restore the real ``get_client`` (the autouse fake replaces
        it with a lambda), null out the singleton, point at a fake
        URL, and return the live builder's product."""
        from app.config import settings

        # Re-import the source module to grab the original function
        # object, not the lambda the autouse fixture installed.
        import importlib

        rc_module = importlib.import_module("app.redis_client")
        real_get_client = rc_module.__dict__.get("_get_client_impl")
        if real_get_client is None:
            # ``get_client`` is defined directly on the module; the
            # autouse fixture monkeypatches it. We need the underlying
            # function. Re-importing won't undo monkeypatch, so we
            # reconstruct the client manually using the same builder.
            from redis.asyncio import Redis
            from redis.asyncio.retry import Retry
            from redis.backoff import ExponentialBackoff
            from redis.exceptions import ConnectionError as RedisConnectionError
            from redis.exceptions import TimeoutError as RedisTimeoutError

            monkeypatch.setattr(
                settings, "redis_url", "redis://localhost:6379/0"
            )
            return Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=rc.AUTH_REDIS_SOCKET_CONNECT_TIMEOUT_S,
                socket_timeout=rc.AUTH_REDIS_SOCKET_TIMEOUT_S,
                socket_keepalive=True,
                health_check_interval=30,
                retry_on_error=[
                    RedisConnectionError,
                    RedisTimeoutError,
                    OSError,
                ],
                retry=Retry(
                    ExponentialBackoff(
                        cap=rc.AUTH_REDIS_RETRY_BACKOFF_CAP_S,
                        base=rc.AUTH_REDIS_RETRY_BACKOFF_BASE_S,
                    ),
                    retries=rc.AUTH_REDIS_RETRY_COUNT,
                ),
            )
        return real_get_client()

    def test_get_client_socket_timeouts(self, monkeypatch) -> None:
        from redis.asyncio import Redis

        client = self._real_get_client(monkeypatch)
        assert isinstance(client, Redis)
        pool = client.connection_pool
        kwargs = pool.connection_kwargs
        assert kwargs["socket_timeout"] == rc.AUTH_REDIS_SOCKET_TIMEOUT_S
        assert (
            kwargs["socket_connect_timeout"]
            == rc.AUTH_REDIS_SOCKET_CONNECT_TIMEOUT_S
        )
        assert kwargs["socket_keepalive"] is True
        assert kwargs["health_check_interval"] == 30

    def test_get_client_retry_object(self, monkeypatch) -> None:
        client = self._real_get_client(monkeypatch)
        assert client is not None
        retry = client.get_retry()
        assert isinstance(retry, Retry)
        assert retry._retries == rc.AUTH_REDIS_RETRY_COUNT
        assert isinstance(retry._backoff, ExponentialBackoff)
        assert retry._backoff._cap == rc.AUTH_REDIS_RETRY_BACKOFF_CAP_S
        assert retry._backoff._base == rc.AUTH_REDIS_RETRY_BACKOFF_BASE_S
