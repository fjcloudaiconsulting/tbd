"""Auth Redis client fail-fast budget — 2026-05-19.

Production trace at 2026-05-19T15:25-15:42 showed ``/auth/refresh``
hanging 45 s with `(canceled)` in the browser network panel. Root
cause: the auth Redis client's pre-fix budget gave each Redis call up
to ~17 s of honest worst-case retry latency (``socket_timeout=3``,
``retries=2``, ``ExponentialBackoff(cap=1.0)``, plus reconnect cost on
each retry because ``retry_on_error`` drops the connection). After
PR #315 ``/refresh`` makes up to 7 sequential Redis calls in the
already_rotated branch, so worst case was ~119 s — well past the
frontend's 45 s reactive-recovery cancel.

After this fix: per-call worst case ``1 + 1 * (1 + 1 + 0.2) = 3.2 s``
honest (accounting for reconnect on retry). ``/refresh`` worst case:

  - Normal "ok" rotation, 3 calls: ~9.6 s
  - Direct grace branch, 5 calls: ~16 s
  - already_rotated branch, 7 calls: ~22.4 s

All under the frontend's 45 s cancel, with ~20 s margin even in the
pathological already_rotated path. A transient VPC blip now surfaces
as a fail-fast 503 the frontend retries, not a hung "(canceled)".

These tests pin both the budget constants AND the live
``_build_auth_redis_client`` product (the builder that ``get_client``
delegates to). They do NOT exercise wire-level behaviour against a
real Redis — that's covered by ``test_redis_transport_normalizer`` and
the integration suite.
"""
from __future__ import annotations

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff

from app import redis_client as rc


# Capture the real ``get_client`` BEFORE the conftest autouse fixture
# monkeypatches the module attribute with a fake-Redis lambda. The
# autouse fixture only runs per-test; the module-import-time reference
# here keeps a pointer to the production function we can call below
# without ``del sys.modules`` (which leaves the module state corrupted
# for any test running after this file).
_REAL_GET_CLIENT = rc.get_client


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

    def test_honest_per_call_budget_under_four_seconds(self) -> None:
        """Honest per-call worst case. redis-py's ``retry_on_error``
        contract drops the connection and reconnects on listed
        exception classes, so the retry attempt pays
        ``socket_connect_timeout`` AGAIN. The earlier version of this
        test ignored that cost (architect P2 on 2026-05-19); honest
        formula:

            socket_timeout + retries * (socket_connect_timeout
                                        + socket_timeout
                                        + backoff_cap)
        """
        per_call_budget = (
            rc.AUTH_REDIS_SOCKET_TIMEOUT_S
            + rc.AUTH_REDIS_RETRY_COUNT
            * (
                rc.AUTH_REDIS_SOCKET_CONNECT_TIMEOUT_S
                + rc.AUTH_REDIS_SOCKET_TIMEOUT_S
                + rc.AUTH_REDIS_RETRY_BACKOFF_CAP_S
            )
        )
        # Current values give 3.2 s; assert under 4 s with a small
        # margin so future minor tuning doesn't trip this without
        # blowing the bound wide open.
        assert per_call_budget < 4.0, (
            f"Per-call Redis budget exceeded 4 s: {per_call_budget}. "
            "Re-tune AUTH_REDIS_* constants or document an explicit "
            "deviation with operator approval."
        )

    def test_refresh_worst_case_path_under_30_seconds(self) -> None:
        """``/auth/refresh`` post-PR #315 makes up to **7 sequential
        Redis calls** in the already_rotated branch (validator's
        validate + family_member + rotation Lua + grace re-probe +
        family_exists re-probe + catch-up's validate + family_member).
        Honest worst-case formula must include socket_connect_timeout
        on the retry attempt (architect P2 fix). 30 s is the upper
        ceiling we hold ourselves to so there's at least 15 s of
        margin under the frontend's 45 s reactive-recovery cancel."""
        per_call = (
            rc.AUTH_REDIS_SOCKET_TIMEOUT_S
            + rc.AUTH_REDIS_RETRY_COUNT
            * (
                rc.AUTH_REDIS_SOCKET_CONNECT_TIMEOUT_S
                + rc.AUTH_REDIS_SOCKET_TIMEOUT_S
                + rc.AUTH_REDIS_RETRY_BACKOFF_CAP_S
            )
        )
        # Branch call counts must mirror the docstring in
        # backend/app/redis_client.py get_client. If a new Redis call
        # is added to /refresh, bump this number AND verify the bound
        # still holds.
        ALREADY_ROTATED_CALLS = 7
        worst_case = ALREADY_ROTATED_CALLS * per_call
        assert worst_case < 30.0, (
            f"Worst-case /refresh already_rotated budget {worst_case}s "
            "exceeds the 30 s self-imposed ceiling. Either tighten the "
            "AUTH_REDIS_* constants or shorten the /refresh call chain."
        )
        # And the absolute hard ceiling: frontend cancels at 45 s.
        FRONTEND_CANCEL_BUDGET_S = 45.0
        assert worst_case < FRONTEND_CANCEL_BUDGET_S - 10, (
            f"Worst-case /refresh budget {worst_case}s leaves <10 s "
            f"margin under the frontend's {FRONTEND_CANCEL_BUDGET_S}s "
            "cancel — the user-visible hang class returns."
        )

    def test_direct_grace_branch_worst_case_under_20_seconds(
        self,
    ) -> None:
        """Direct grace branch is 5 Redis calls (validate + grace +
        family_exists + catch-up validate + catch-up family_member).
        Less calls than already_rotated; bound is correspondingly
        tighter."""
        per_call = (
            rc.AUTH_REDIS_SOCKET_TIMEOUT_S
            + rc.AUTH_REDIS_RETRY_COUNT
            * (
                rc.AUTH_REDIS_SOCKET_CONNECT_TIMEOUT_S
                + rc.AUTH_REDIS_SOCKET_TIMEOUT_S
                + rc.AUTH_REDIS_RETRY_BACKOFF_CAP_S
            )
        )
        DIRECT_GRACE_CALLS = 5
        assert DIRECT_GRACE_CALLS * per_call < 20.0


# ── Live client construction reflects the budget ────────────────────────


class TestBuilderConstructsBudgetedClient:
    """``_build_auth_redis_client`` is the real builder ``get_client``
    delegates to. Tests call it directly so they DO exercise the
    production code path (architect P2 on 2026-05-19: the earlier
    version reconstructed ``Redis.from_url`` manually, which would
    pass even if ``get_client`` drifted to a different config)."""

    def test_builder_applies_socket_timeouts(self) -> None:
        """The builder's resulting ``Redis`` must carry exactly the
        configured socket timeouts AND keepalive AND health check
        interval — drift in any one of these silently re-inflates the
        per-call budget."""
        client = rc._build_auth_redis_client("redis://localhost:6379/0")
        assert isinstance(client, Redis)
        kwargs = client.connection_pool.connection_kwargs
        assert kwargs["socket_timeout"] == rc.AUTH_REDIS_SOCKET_TIMEOUT_S
        assert (
            kwargs["socket_connect_timeout"]
            == rc.AUTH_REDIS_SOCKET_CONNECT_TIMEOUT_S
        )
        assert kwargs["socket_keepalive"] is True
        assert kwargs["health_check_interval"] == 30

    def test_builder_applies_retry_with_exponential_backoff(self) -> None:
        """The retry object must carry the configured ``retries`` and
        an ``ExponentialBackoff`` with the configured ``cap`` and
        ``base``. redis-py stores these on underscore-prefixed
        attributes in 5.x."""
        client = rc._build_auth_redis_client("redis://localhost:6379/0")
        retry = client.get_retry()
        assert isinstance(retry, Retry)
        assert retry._retries == rc.AUTH_REDIS_RETRY_COUNT
        assert isinstance(retry._backoff, ExponentialBackoff)
        assert retry._backoff._cap == rc.AUTH_REDIS_RETRY_BACKOFF_CAP_S
        assert retry._backoff._base == rc.AUTH_REDIS_RETRY_BACKOFF_BASE_S

    def test_get_client_delegates_to_builder(self, monkeypatch) -> None:
        """``get_client`` MUST go through ``_build_auth_redis_client``.
        If a future refactor inlines the construction back into
        ``get_client``, this test catches it — drift between the two
        construction paths is exactly what the architect's P2 review
        warned about."""
        from app.config import settings

        call_log: list[str] = []

        def _spy_builder(url: str) -> Redis:
            call_log.append(url)
            return Redis.from_url(url, decode_responses=True)

        # Patch the builder + reset the singleton + pin a recognizable
        # URL, then call the production ``get_client`` reference we
        # captured at import time (the conftest autouse replaced the
        # module attribute with a fake-Redis lambda).
        monkeypatch.setattr(rc, "_build_auth_redis_client", _spy_builder)
        monkeypatch.setattr(rc, "_client", None)
        monkeypatch.setattr(settings, "redis_url", "redis://probe:6379/0")

        client = _REAL_GET_CLIENT()
        assert client is not None
        assert call_log == ["redis://probe:6379/0"], (
            f"get_client must delegate to _build_auth_redis_client; "
            f"spy log: {call_log!r}"
        )
