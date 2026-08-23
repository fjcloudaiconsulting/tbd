"""Fences for ``GET /health/dependencies`` (TBD-413).

## What this endpoint exists to stop

On 2026-08-19, during the TBD-360 cutover, Redis was enforcing a stale
password. Every login returned 503 — ``auth.py`` fails CLOSED on unreachable
Redis, which is correct — while ``/ready`` returned
``200 {"status":"ready","database":"connected"}``. Every external signal said
healthy on an app where nobody could log in.

Reproduced live on 2026-08-22 before this change existed::

    /health: (200, '{"status":"ok"}')
    /ready : (200, '{"status":"ready","database":"connected"}')
    /login : (503, '{"detail":"Authentication temporarily unavailable"}')

## Why a new endpoint instead of making ``/ready`` 503

``/ready`` is the ROTATION gate. ``k8s/templates/backend.yaml:56`` points a
readinessProbe at it, and Redis is a single shared instance, so a Redis outage
would fail every replica's readiness simultaneously and evict the whole
deployment — including the data plane, which does not need Redis at all
(``app/deps.py`` has zero Redis references and access tokens live 15 minutes).
``.github/workflows/test.yml``'s ``Migration Checks`` also boots the app with
no ``REDIS_URL`` and asserts ``/ready`` is 200, and it feeds the REQUIRED
``Backend Checks`` gate.

So ``/ready`` keeps its response contract byte-identical and this endpoint
carries the per-dependency truth. F10 is the fence that keeps that true.

## Reading these tests

⚠ Never rely on ambient ``settings.redis_url``. It is
``redis://redis:6379/0`` inside the dev container and ``""`` on the CI
shards, so an ambient-dependent test passes in one and fails in the other.
Every test here monkeypatches ``get_client`` and ``app_env`` explicitly.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import AuthenticationError, ConnectionError as RedisConnectionError

from app import redis_client
from app.config import settings


DEPS = "/health/dependencies"


@pytest.fixture
def client():
    """A TestClient that does NOT enter the lifespan.

    ``app.main``'s lifespan runs migrations and can start the scheduler.
    ``TestClient(app)`` used as a plain object (no ``with``) never triggers
    startup, which is what we want: these tests exercise route handlers.
    """
    from app.main import app

    return TestClient(app)


class _Pinger:
    """Stand-in Redis client whose ``ping`` does exactly one scripted thing."""

    def __init__(self, *, result=True, raises=None, hang=False):
        self._result = result
        self._raises = raises
        self._hang = hang
        self.ping_calls = 0

    async def ping(self):
        self.ping_calls += 1
        if self._hang:
            await asyncio.sleep(3600)
        if self._raises is not None:
            raise self._raises
        return self._result


def _set_redis(monkeypatch, obj):
    monkeypatch.setattr(redis_client, "get_client", lambda: obj)


def _set_env(monkeypatch, env: str):
    monkeypatch.setattr(settings, "app_env", env)


@pytest.fixture(autouse=True)
def healthy_db(monkeypatch):
    """Every test starts from a WORKING database, substituted in-process.

    ⚠ Not a convenience — a correctness requirement. ``TestClient`` drives
    each request through a fresh event-loop portal, while ``engine`` is a
    module-level ``AsyncEngine`` whose pooled connections bind to the loop
    that created them. Reusing the real engine across requests raises
    ``got Future attached to a different loop`` and the DB probe reports
    ``unreachable`` for reasons that have nothing to do with the code under
    test. The CI shards also run with no MySQL at all, so an
    ambient-database test could not pass there regardless.

    Tests that want a broken database call ``_break_db`` and override this.
    """
    _set_db_ok(monkeypatch)


def _set_db_ok(monkeypatch):
    from app import main as app_main

    class _OkConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, *a, **k):
            return None

    class _OkEngine:
        def connect(self):
            return _OkConn()

    monkeypatch.setattr(app_main, "engine", _OkEngine())


def _break_db(monkeypatch, exc=None, hang=False):
    """Make the shared engine fail or hang on connect().

    ⚠ Replaces the whole ``app.main.engine`` NAME rather than setting
    ``engine.connect``: ``AsyncEngine.connect`` is a read-only attribute and
    monkeypatch cannot restore it, which surfaces as a teardown
    ``AttributeError`` on every test that tries. ``main.py`` does
    ``from app.database import engine``, so the module-level name is the
    patchable seam — and it is the same name both ``/ready`` and the probe
    read, which is what makes F10 meaningful.
    """
    from app import main as app_main

    class _BadConn:
        async def __aenter__(self):
            if hang:
                await asyncio.sleep(3600)
            raise exc if exc is not None else OSError("db down")

        async def __aexit__(self, *a):
            return False

    class _BadEngine:
        def connect(self):
            return _BadConn()

    monkeypatch.setattr(app_main, "engine", _BadEngine())


# ── F1-F9: the state matrix ────────────────────────────────────────────────


def test_f1_database_ok_and_redis_ok_is_200(client, monkeypatch):
    """F1 — the healthy baseline."""
    _set_redis(monkeypatch, _Pinger())
    _set_env(monkeypatch, "production")

    r = client.get(DEPS)

    assert r.status_code == 200, r.text
    assert r.json() == {
        "status": "ok",
        "checks": {"database": "ok", "redis": "ok"},
    }


def test_f2_redis_unreachable_is_503(client, monkeypatch):
    """F2 — THE SHIPPED BUG. Redis down must not report healthy.

    Kills: reporting 200 while the session store is unreachable, which is
    exactly what ``/ready`` did on 2026-08-19.
    """
    _set_redis(monkeypatch, _Pinger(raises=RedisConnectionError("no route")))
    _set_env(monkeypatch, "production")

    r = client.get(DEPS)

    assert r.status_code == 503, r.text
    body = r.json()
    assert body["status"] == "unhealthy", body
    assert body["checks"]["redis"] == "unreachable", body
    assert body["checks"]["database"] == "ok", body


def test_f3_redis_unreachable_is_503_in_development_too(client, monkeypatch):
    """F3 — configured-but-unreachable is 503 in EVERY environment.

    Kills the env-gated predicate ``200 iff db ok and (redis ok or
    app_env != "production")``, which one architect wrote in prose and which
    contradicted its own table: it returns 200 in dev with Redis down, the
    precise state reproduced in this ticket. Only the EMPTY-``redis_url`` row
    is environment-sensitive; an unreachable configured Redis never is.
    """
    _set_redis(monkeypatch, _Pinger(raises=RedisConnectionError("no route")))
    _set_env(monkeypatch, "development")

    r = client.get(DEPS)

    assert r.status_code == 503, r.text
    assert r.json()["checks"]["redis"] == "unreachable"


def test_f4_redis_auth_failure_reports_auth_failed_not_unreachable(client, monkeypatch):
    """F4 — credential failure must be distinguishable from a network failure.

    ``AuthenticationError`` SUBCLASSES ``ConnectionError`` (verified in F4b),
    so an ``except ConnectionError`` placed first silently collapses a
    credential incident into "unreachable" and sends the operator hunting a
    network problem. TBD-412 / the 2026-08-19 incident WAS a credential
    failure, and the ticket's own example body asks for ``auth_failed``.

    Kills: except-clause ordering with ``ConnectionError`` before
    ``AuthenticationError``.
    """
    _set_redis(monkeypatch, _Pinger(raises=AuthenticationError("bad password")))
    _set_env(monkeypatch, "production")

    r = client.get(DEPS)

    assert r.status_code == 503, r.text
    assert r.json()["checks"]["redis"] == "auth_failed", r.json()


def test_f4b_authenticationerror_really_does_subclass_connectionerror():
    """F4b — the premise F4 rests on, asserted rather than assumed.

    If a future redis-py makes these siblings, F4's ordering rationale
    evaporates and this fence explains why the code looks the way it does.
    """
    assert issubclass(AuthenticationError, RedisConnectionError)


def test_f5_redis_unconfigured_outside_production_is_200_disabled(client, monkeypatch):
    """F5 — an empty ``redis_url`` outside production is a SUPPORTED mode.

    Kills: treating unconfigured Redis as a failure. That would turn
    ``Migration Checks`` — which sets no ``REDIS_URL`` and feeds the REQUIRED
    ``Backend Checks`` gate — red on 100% of runs.
    """
    _set_redis(monkeypatch, None)
    _set_env(monkeypatch, "development")

    r = client.get(DEPS)

    assert r.status_code == 200, r.text
    assert r.json() == {
        "status": "ok",
        "checks": {"database": "ok", "redis": "disabled"},
    }


def test_f6_redis_unconfigured_in_production_is_503_not_configured(client, monkeypatch):
    """F6 — the same observation is a FAILURE in production, and is named
    differently so the body explains itself without the reader knowing
    ``app_env``.

    Kills two things: treating prod-without-Redis as fine, AND collapsing
    ``disabled`` and ``not_configured`` into one string (F5 + F6 together
    pin both names).
    """
    _set_redis(monkeypatch, None)
    _set_env(monkeypatch, "production")

    r = client.get(DEPS)

    assert r.status_code == 503, r.text
    assert r.json() == {
        "status": "unhealthy",
        "checks": {"database": "ok", "redis": "not_configured"},
    }


def test_f7_database_down_still_reports_redis(client, monkeypatch):
    """F7 — no short-circuiting. A failing DB must not erase the Redis answer.

    Kills: ``if db_failed: return early``. Mid-incident, "is Redis also gone?"
    is exactly the question this endpoint exists to answer.
    """
    _break_db(monkeypatch)
    _set_redis(monkeypatch, _Pinger())
    _set_env(monkeypatch, "production")

    r = client.get(DEPS)

    assert r.status_code == 503, r.text
    body = r.json()
    assert body["checks"]["database"] == "unreachable", body
    assert body["checks"]["redis"] == "ok", body


def test_f8_both_down_reports_both(client, monkeypatch):
    """F8 — F7's mirror: both failing must still produce both values."""
    _break_db(monkeypatch)
    _set_redis(monkeypatch, _Pinger(raises=RedisConnectionError("no route")))
    _set_env(monkeypatch, "production")

    r = client.get(DEPS)

    assert r.status_code == 503, r.text
    body = r.json()
    assert body["checks"]["database"] == "unreachable", body
    assert body["checks"]["redis"] == "unreachable", body


def test_f9_hanging_redis_times_out_without_losing_the_database_answer(
    client, monkeypatch
):
    """F9 — a hung Redis must be bounded by its OWN probe timeout.

    Kills: removing the per-probe ``asyncio.wait_for`` on ``ping()``.

    ⚠ The obvious version of this fence does NOT discriminate, and shipping
    it would have been a vacuous test. Asserting only
    ``checks["redis"] == "timeout"`` still passes with the per-probe bound
    deleted, because the OUTER backstop catches the hang and reports BOTH
    checks as ``timeout`` — a downstream guard masking the mutant. Measured:
    the mutant passed that assertion in 6.84s.

    The discriminator is what the two bounds do DIFFERENTLY. A per-probe
    bound isolates the failure and preserves the other probe's real answer;
    the backstop cannot, because it cancels the whole gather. So this asserts
    the database is still ``ok`` — which is also the property the endpoint
    exists for, since "is the DB fine while Redis is gone?" is the question
    an operator is asking. The wall-clock bound is a second, independent kill.
    """
    import time

    from app import main as app_main

    monkeypatch.setattr(app_main, "_REDIS_PROBE_TIMEOUT_S", 0.05)
    _set_redis(monkeypatch, _Pinger(hang=True))
    _set_env(monkeypatch, "production")

    started = time.monotonic()
    r = client.get(DEPS)
    elapsed = time.monotonic() - started

    assert r.status_code == 503, r.text
    body = r.json()
    assert body["checks"]["redis"] == "timeout", body
    assert body["checks"]["database"] == "ok", (
        "the database answer was lost, so the hang was caught by the outer "
        f"backstop rather than the per-probe bound: {body}"
    )
    assert elapsed < 1.0, (
        f"probe took {elapsed:.2f}s; the per-probe Redis bound (patched to "
        f"0.05s) is not being applied, so the {app_main._DEPS_PROBE_TOTAL_TIMEOUT_S}s "
        "backstop absorbed it instead"
    )


def test_f9b_hanging_database_times_out(client, monkeypatch):
    """F9b — the same for the database side.

    ⚠ This bound is the ONLY one on the query: per ``database.py:22-31``
    aiomysql 0.2.0 accepts no ``read_timeout``, so ``connect_timeout`` covers
    establishment only and a wedged established socket has no driver bound.
    """
    from app import main as app_main

    monkeypatch.setattr(app_main, "_DB_PROBE_TIMEOUT_S", 0.05)
    _break_db(monkeypatch, hang=True)
    _set_redis(monkeypatch, _Pinger())
    _set_env(monkeypatch, "production")

    r = client.get(DEPS)

    assert r.status_code == 503, r.text
    body = r.json()
    assert body["checks"]["database"] == "timeout", body
    assert body["checks"]["redis"] == "ok", body


# ── F10-F11: the endpoints that must NOT change ────────────────────────────


def test_f10_ready_is_unchanged_when_redis_is_down(client, monkeypatch):
    """F10 — ``/ready`` keeps its contract byte-identical with Redis down.

    Kills the tempting "helpful" edit: adding the Redis check to ``/ready``.
    That would evict every k8s replica on a shared-Redis outage and turn the
    required ``Migration Checks`` gate red, which is the whole reason this
    ticket built a separate endpoint.
    """
    _set_redis(monkeypatch, _Pinger(raises=RedisConnectionError("no route")))
    _set_env(monkeypatch, "production")

    r = client.get("/ready")

    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ready", "database": "connected"}


def test_f11_health_is_pure_liveness_with_everything_down(client, monkeypatch):
    """F11 — ``/health`` must never depend on anything external."""
    _break_db(monkeypatch)
    _set_redis(monkeypatch, _Pinger(raises=RedisConnectionError("no route")))
    _set_env(monkeypatch, "production")

    r = client.get("/health")

    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok"}


# ── F12-F13: the probe must be a pure observer ─────────────────────────────


def test_f12_probe_never_constructs_a_redis_client(client, monkeypatch):
    """F12 — the probe uses the SHARED singleton, never a fresh client.

    Kills: ``Redis.from_url()`` per request, which would allocate a pool on
    every scrape of an unauthenticated endpoint, and would measure a
    connection path production never takes.
    """
    def _boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("probe constructed a Redis client")

    monkeypatch.setattr(redis_client, "_build_auth_redis_client", _boom)
    _set_redis(monkeypatch, _Pinger())
    _set_env(monkeypatch, "production")

    r = client.get(DEPS)

    assert r.status_code == 200, r.text


def test_f13_failing_probe_does_not_retire_the_shared_singleton(client, monkeypatch):
    """F13 — a failed probe must not tear down the live auth pool.

    Kills: routing the probe through a ``@_normalize_transport_errors``
    helper. That decorator calls ``_retire_poisoned_client``, so a monitoring
    scrape against a blipping Redis would drop the singleton that real auth
    traffic is using.
    """
    sentinel = _Pinger(raises=RedisConnectionError("transport closed"))
    monkeypatch.setattr(redis_client, "_client", sentinel, raising=False)
    _set_redis(monkeypatch, sentinel)
    _set_env(monkeypatch, "production")

    r = client.get(DEPS)

    assert r.status_code == 503, r.text
    assert redis_client._client is sentinel, "probe retired the shared client"


def test_f14_redis_py_still_disconnects_on_cancellation():
    """F14 — the pinned-library guarantee this design rests on.

    ``asyncio.wait_for`` cancelling a ``ping()`` is only safe because
    redis-py tears the connection down on ``BaseException`` before
    re-raising, so a cancelled probe cannot leave a half-read reply in a
    pooled connection that ``session_validate`` then picks up.

    Next-upgrade checklist (redis==5.2.1, verified 2026-08-22):
      * ``redis/asyncio/connection.py`` ``AbstractConnection.read_response``
        — ``except BaseException:`` -> ``await self.disconnect(nowait=True)``
        (cites redis-py issue #1128)
      * same guard on the write side in ``send_packed_command``
      * ``ConnectionPool.ensure_connection`` re-checks with
        ``can_read_destructive()`` on every checkout

    Asserted by PARSING the signature, not grepping the file: a grep for
    ``disconnect_on_error`` is satisfied by the comment that documents it.
    """
    from redis.asyncio.connection import AbstractConnection

    param = inspect.signature(AbstractConnection.read_response).parameters[
        "disconnect_on_error"
    ]
    assert param.default is True, (
        "redis-py no longer disconnects on error by default; the "
        "cancellation-safety argument for probing the shared singleton "
        "needs re-deriving before this endpoint can keep using it."
    )


def test_f15_response_never_leaks_exception_detail(client, monkeypatch):
    """F15 — this endpoint is unauthenticated. Coarse strings only.

    Kills: ``str(exc)`` in the body, which would publish hostnames, ports,
    driver messages and occasionally credentials to anonymous callers.
    """
    secret = "redis://:hunter2@10.42.9.9:6379/0 connection refused"
    _break_db(monkeypatch, exc=OSError("mysql://root:s3cr3t@10.42.0.5:3306 refused"))
    _set_redis(monkeypatch, _Pinger(raises=RedisConnectionError(secret)))
    _set_env(monkeypatch, "production")

    r = client.get(DEPS)

    raw = r.text
    for leak in ("hunter2", "s3cr3t", "10.42", "6379", "3306", "refused"):
        assert leak not in raw, f"response leaked {leak!r}: {raw}"


def test_f16_allowed_vocabulary_only(client, monkeypatch):
    """F16 — guard: values stay inside the documented closed vocabulary."""
    _set_redis(monkeypatch, _Pinger())
    _set_env(monkeypatch, "production")

    body = client.get(DEPS).json()

    assert body["status"] in {"ok", "unhealthy"}
    assert body["checks"]["database"] in {"ok", "unreachable", "timeout"}
    assert body["checks"]["redis"] in {
        "ok", "unreachable", "timeout", "auth_failed", "disabled", "not_configured",
    }
