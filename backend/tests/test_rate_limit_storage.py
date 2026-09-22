"""Regression tests for slowapi Limiter storage selection (K8S-1).

L0.6 multi-replica readiness audit (2026-05-08) flagged in-memory
rate-limit storage as a bug for horizontal scale: counters are per-
process, so each replica enforces its own private budget. K8S-1
points the limiter at Redis when configured (shared budget across
replicas) and falls back to in-memory only when ``settings.redis_url``
is empty.

These tests pin the construction-time wiring of ``_build_limiter`` so
the choice is observable without standing up Redis in CI:

- With ``redis_url`` set: the Limiter is built with a Redis storage
  URI and its underlying ``limits`` storage class is the Redis one.
- With ``redis_url`` empty: the Limiter falls back to the default
  in-memory ``MemoryStorage`` and a warning is logged.

The Redis case here only inspects construction (no live Redis call);
the manual smoke test in the PR body covers the end-to-end persist-
across-restart behaviour.
"""
from __future__ import annotations

from limits.storage import MemoryStorage

from app import rate_limit
from app.config import settings


def test_limiter_uses_redis_storage_when_redis_url_set(monkeypatch):
    """K8S-1: with ``settings.redis_url`` set, the Limiter is built
    against the Redis storage backend so counters are shared across
    replicas.

    The Redis storage is wrapped by ``FailOpenRedisStorage`` (prod
    hotfix 2026-05-13) so transient Redis blips do not surface as
    HTTP 500. Assert on both the wrapper presence AND the wrapped
    inner type so both layers stay pinned.
    """
    monkeypatch.setattr(settings, "redis_url", "redis://example:6379/0")

    limiter = rate_limit._build_limiter()

    # slowapi defers storage creation until first access via the
    # ``_storage`` property. Touching it forces the limits library to
    # resolve the URI; we then assert it picked the Redis backend.
    from app.rate_limit_failopen import FailOpenRedisStorage

    storage = limiter._storage
    assert isinstance(storage, FailOpenRedisStorage), (
        f"expected fail-open wrapper, got {type(storage).__name__}"
    )
    inner = storage._inner
    assert type(inner).__name__ == "RedisStorage", (
        f"expected Redis-backed inner storage, got {type(inner).__name__}"
    )

    # Production hotfix 2026-05-15. Without these socket-level options the
    # limits-library Redis client defaults to socket_timeout=None — an
    # infinite blocking read on the sync slowapi storage path. Since
    # slowapi evaluates limits synchronously inside async request handlers,
    # one flaky Redis socket parks the entire uvicorn event loop and every
    # route (including /health) stops responding. Pin connection_kwargs
    # here so future refactors cannot silently regress the cap. See
    # ``app/rate_limit.py:_build_limiter`` for the full rationale.
    redis_client = inner.storage
    kwargs = redis_client.connection_pool.connection_kwargs
    assert kwargs["socket_timeout"] == 1, (
        f"socket_timeout must be 1s to prevent event-loop block, "
        f"got {kwargs.get('socket_timeout')!r}"
    )
    assert kwargs["socket_connect_timeout"] == 1, (
        f"socket_connect_timeout must be 1s to prevent event-loop block, "
        f"got {kwargs.get('socket_connect_timeout')!r}"
    )
    assert kwargs["socket_keepalive"] is True, (
        f"socket_keepalive must be True for connection hygiene, "
        f"got {kwargs.get('socket_keepalive')!r}"
    )
    assert kwargs["health_check_interval"] == 30, (
        f"health_check_interval must be 30s, "
        f"got {kwargs.get('health_check_interval')!r}"
    )


def test_limiter_falls_back_to_memory_when_redis_url_empty(monkeypatch):
    """When ``settings.redis_url`` is empty (e.g. local dev without
    the compose Redis service), the Limiter keeps the in-memory
    backend and surfaces a warning so the gap is visible in logs.

    ⚠ The warning is observed by binding a recorder onto the module's own
    ``logger``, NOT by sampling ``capsys``. Rendered-output sampling made
    this test order-dependent: ``app.main`` calls ``setup_logging()`` at
    import, which swaps structlog onto the stdlib bridge with a
    ``StreamHandler`` holding the ``sys.stdout`` of import time — invisible
    to ``capsys``, and JSON-rendered rather than ``backend=memory``. It only
    ever passed because ``tests/test_migrate_script.py`` happens to call
    ``structlog.reset_defaults()`` and happens to sort immediately before
    this file. Any run that separates them (``pytest -n``, ``-k``, a rename)
    turned it red. Same remedy as ``tests/auth/test_anonymous_audit_bounds``.
    """
    monkeypatch.setattr(settings, "redis_url", "")

    warnings: list[tuple[str, dict]] = []

    class _Recorder:
        def info(self, event, **kw):
            pass

        def warning(self, event, **kw):
            warnings.append((event, kw))

    monkeypatch.setattr(rate_limit, "logger", _Recorder())

    limiter = rate_limit._build_limiter()

    storage = limiter._storage
    assert isinstance(storage, MemoryStorage), (
        f"expected in-memory fallback, got {type(storage).__name__}"
    )
    # Warning must carry the rate-limit storage event so ops can grep.
    assert [event for event, _ in warnings] == ["rate_limit.storage"], (
        f"expected exactly one rate_limit.storage warning, got {warnings!r}"
    )
    assert warnings[0][1]["backend"] == "memory", (
        f"expected the fallback warning to identify backend=memory, "
        f"got {warnings[0][1]!r}"
    )


def test_module_level_limiter_built_at_import_time():
    """Smoke: the module-level ``limiter`` exposed to routers is the
    object built by ``_build_limiter`` at import time, not a stale
    placeholder. This is what ``app.state.limiter`` and every router's
    ``@limiter.limit(...)`` decorator binds to.
    """
    assert rate_limit.limiter is not None
    # ``key_func`` must still be the topology-aware resolver from PR #233.
    assert rate_limit.limiter._key_func is rate_limit.get_client_ip
