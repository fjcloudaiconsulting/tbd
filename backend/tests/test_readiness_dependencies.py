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

``/ready`` is the ROTATION gate. The platform readiness probe points a
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

import ast
import asyncio
import inspect
import pathlib

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import (
    AuthenticationError,
    ConnectionError as RedisConnectionError,
    NoPermissionError,
    TimeoutError as RedisTimeoutError,
)

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


def test_f4c_redis_acl_denial_reports_auth_failed_not_unreachable(client, monkeypatch):
    """F4c — a lost Redis ACL grant is a CREDENTIAL failure, not a network one.

    ``NoPermissionError`` is what redis-py raises for ``NOPERM`` — the
    credential authenticated, but the ACL no longer permits the command. It is
    a ``ResponseError`` -> ``RedisError`` and NOT a ``ConnectionError``
    (asserted in F4d), so unlike ``AuthenticationError`` it does not ride F4's
    subclass ordering: without being named explicitly it lands in the
    ``(RedisConnectionError, RedisError, OSError)`` tuple and reports
    ``unreachable``.

    Kills: dropping ``RedisNoPermissionError`` from the ``auth_failed``
    clause, which sends an operator hunting a network fault for a permissions
    change — the exact misdiagnosis F4 exists to prevent, one rung over.
    """
    _set_redis(
        monkeypatch,
        _Pinger(raises=NoPermissionError("NOPERM this user has no permissions")),
    )
    _set_env(monkeypatch, "production")

    r = client.get(DEPS)

    assert r.status_code == 503, r.text
    assert r.json()["checks"]["redis"] == "auth_failed", r.json()


def test_f4d_nopermissionerror_is_not_a_connectionerror():
    """F4d — the premise F4c rests on, asserted rather than assumed.

    If it ever becomes a ``ConnectionError`` subclass it would be caught by
    F4's clause for free and F4c's separate name would be dead weight; if it
    stops being a ``RedisError`` the fallback analysis changes too.
    """
    assert not issubclass(NoPermissionError, RedisConnectionError)
    from redis.exceptions import RedisError

    assert issubclass(NoPermissionError, RedisError)


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


def test_f9c_redis_protocol_timeout_reports_timeout_not_unreachable(
    client, monkeypatch
):
    """F9c — ``redis.exceptions.TimeoutError`` is its OWN state.

    Distinct from F9: F9 covers a ping that never returns and is cut off by
    ``asyncio.wait_for``; this covers redis-py's own ``socket_timeout`` firing
    and raising, which is the shape production actually sees first (1.0s
    socket_timeout, well under the probe bound).

    Kills: deleting the ``except RedisTimeoutError`` clause. Without it the
    state collapses into ``unreachable`` via the ``RedisError`` tuple below
    it, every other test stays green, and the operator loses the distinction
    between "Redis answered too slowly" and "Redis is not there".
    """
    _set_redis(monkeypatch, _Pinger(raises=RedisTimeoutError("timed out")))
    _set_env(monkeypatch, "production")

    r = client.get(DEPS)

    assert r.status_code == 503, r.text
    assert r.json()["checks"]["redis"] == "timeout", r.json()


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


def test_f12_probe_pings_the_shared_singleton_and_nothing_else(client, monkeypatch):
    """F12 — the probe uses the SHARED singleton, never a fresh client.

    Kills: ``Redis.from_url()`` per request, which would allocate a pool on
    every scrape of an unauthenticated endpoint and would measure a connection
    path production never takes; ``require_client()``, which raises rather
    than reporting; and any wrapped helper that goes somewhere else entirely.

    ⚠ THE DISCRIMINATOR IS ``ping_calls``, and it has to be. The obvious
    version of this fence patched ``redis_client._build_auth_redis_client``
    with a boom and asserted only ``200``. That patch is structurally
    unreachable — the test also patches ``get_client``, which is the ONLY
    caller of the builder — so the boom could never fire and the surviving
    assertion, ``status_code == 200``, is produced by the healthy fixture no
    matter where the probe got its client from. Asserting the substituted
    singleton was pinged EXACTLY ONCE is what actually kills all three
    mutants: any of them would leave this counter at 0.
    """
    pinger = _Pinger()
    _set_redis(monkeypatch, pinger)
    _set_env(monkeypatch, "production")

    r = client.get(DEPS)

    assert r.status_code == 200, r.text
    assert pinger.ping_calls == 1, (
        "the probe did not ping the shared singleton exactly once "
        f"(ping_calls={pinger.ping_calls}); it built or fetched a client of "
        "its own, so this endpoint is no longer measuring the connection real "
        "auth traffic uses"
    )


def test_f13_failing_probe_does_not_retire_the_shared_singleton(client, monkeypatch):
    """F13 — a failed probe must not tear down the live auth pool.

    Kills: routing the probe through a ``@_normalize_transport_errors``
    helper. That decorator calls ``_retire_poisoned_client``, so a monitoring
    scrape against a blipping Redis would drop the singleton that real auth
    traffic is using.

    ⚠ THE EXCEPTION CLASS IS THE WHOLE FENCE. Raising ``RedisConnectionError``
    here — the obvious choice, and what this test shipped with first — makes
    it VACUOUS: ``_normalize_transport_errors`` matches ``except RedisError:
    raise`` FIRST and re-raises untouched, so it never reaches
    ``_retire_poisoned_client`` and the named mutant survives. That version
    passed against correct code, against the mutant, and against unmodified
    ``main``.

    ``ConnectionResetError`` is the discriminating input: it is an ``OSError``
    subclass, so it hits the decorator's ``except OSError`` leg — the one that
    DOES retire the client — while still landing in the probe's own
    ``except (RedisConnectionError, RedisError, OSError)`` tuple, leaving the
    503 and ``unreachable`` assertions below unchanged. It is also the only
    coverage the ``OSError`` leg of that tuple has.
    """
    sentinel = _Pinger(raises=ConnectionResetError("transport closed"))
    monkeypatch.setattr(redis_client, "_client", sentinel, raising=False)
    _set_redis(monkeypatch, sentinel)
    _set_env(monkeypatch, "production")

    r = client.get(DEPS)

    assert r.status_code == 503, r.text
    assert r.json()["checks"]["redis"] == "unreachable", r.json()
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


# ── F16: the vocabulary is CLOSED, and closed at the source ────────────────


def _sweep_every_state(client, monkeypatch) -> tuple[set[str], set[str], set[str]]:
    """Drive one request per branch the two probes can take.

    Returns ``(statuses, db_states, redis_states)`` — every value the endpoint
    actually produced. ``monkeypatch.undo()`` between scenarios so each starts
    from the autouse healthy database; the fixture's own patch is re-applied
    per scenario rather than relied on.
    """
    from app import main as app_main

    scenarios = [
        # (env, redis client factory, db setup)
        ("production", lambda: _Pinger(), None),
        ("production", lambda: _Pinger(raises=RedisConnectionError("x")), None),
        ("production", lambda: _Pinger(raises=AuthenticationError("x")), None),
        ("production", lambda: _Pinger(raises=NoPermissionError("x")), None),
        ("production", lambda: _Pinger(raises=RedisTimeoutError("x")), None),
        ("production", lambda: _Pinger(hang=True), "redis_hang"),
        ("development", lambda: None, None),
        ("production", lambda: None, None),
        ("production", lambda: _Pinger(), "db_broken"),
        ("production", lambda: _Pinger(), "db_hang"),
    ]

    statuses: set[str] = set()
    db_states: set[str] = set()
    redis_states: set[str] = set()

    for env, make_redis, db_mode in scenarios:
        monkeypatch.undo()
        _set_db_ok(monkeypatch)
        if db_mode == "db_broken":
            _break_db(monkeypatch)
        elif db_mode == "db_hang":
            monkeypatch.setattr(app_main, "_DB_PROBE_TIMEOUT_S", 0.05)
            _break_db(monkeypatch, hang=True)
        elif db_mode == "redis_hang":
            monkeypatch.setattr(app_main, "_REDIS_PROBE_TIMEOUT_S", 0.05)
        _set_redis(monkeypatch, make_redis())
        _set_env(monkeypatch, env)

        body = client.get(DEPS).json()
        statuses.add(body["status"])
        db_states.add(body["checks"]["database"])
        redis_states.add(body["checks"]["redis"])

    return statuses, db_states, redis_states


def test_f16_every_produced_state_is_declared_and_every_declaration_is_reachable(
    client, monkeypatch
):
    """F16 — the closed vocabulary, fenced in BOTH directions.

    ⚠ The version this replaced killed nothing. It drove ONE scenario (the
    healthy one) and asserted membership in a superset containing the single
    value that scenario can produce — while F1 already pinned that whole body
    by strict equality on the identical fixture. Any mutant it could have
    caught was one F1 caught first.

    This drives every branch of both probes and compares the SET of values
    they produce against ``app.main._DB_STATES`` / ``_REDIS_STATES``, which is
    where the contract is now declared. Both directions matter:

      * a state produced but not declared (a new string added to a probe)
        fails the subset direction — a new state must be DECLARED before it
        can be returned, which is the property F16 is supposed to have;
      * a state declared but never produced fails the superset direction, so
        the vocabulary cannot drift into documentation of branches that no
        longer exist.
    """
    from app import main as app_main

    statuses, db_states, redis_states = _sweep_every_state(client, monkeypatch)

    assert statuses == app_main._STATUS_VALUES, (
        f"top-level status values produced {statuses}, declared "
        f"{set(app_main._STATUS_VALUES)}"
    )
    assert db_states == set(app_main._DB_STATES), (
        f"database states produced {db_states}, declared {set(app_main._DB_STATES)}"
    )
    assert redis_states == set(app_main._REDIS_STATES), (
        f"redis states produced {redis_states}, declared "
        f"{set(app_main._REDIS_STATES)}"
    )


def _returned_literals(expr: ast.expr) -> set[str]:
    """The string values a ``return <expr>`` can actually yield.

    ⚠ Deliberately NOT ``ast.walk(return_node)``. That sweeps the whole
    subtree, so ``return "not_configured" if app_env == "production" else
    "disabled"`` reports ``"production"`` — the constant being COMPARED
    against — as a returnable state. Measured: it failed on exactly that.
    Only the value positions count.
    """
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return {expr.value}
    if isinstance(expr, ast.IfExp):
        return _returned_literals(expr.body) | _returned_literals(expr.orelse)
    return set()


def test_f16b_probes_return_no_undeclared_string_literal():
    """F16b — the same closure over the branches F16 does not drive.

    F16 can only see states a test scenario reaches. This one PARSES
    ``app/main.py`` and collects every string literal either probe can
    ``return``, so a new state added on a path no test exercises still has to
    be declared. Parsed, not grepped: a grep for a state name is satisfied by
    the comment naming it.
    """
    from app import main as app_main

    tree = ast.parse(pathlib.Path(inspect.getsourcefile(app_main)).read_text())
    funcs = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name, declared in (
        ("_probe_database", app_main._DB_STATES),
        ("_probe_redis", app_main._REDIS_STATES),
    ):
        assert name in funcs, f"{name} is gone; this fence needs re-pointing"
        literals: set[str] = set()
        for ret in ast.walk(funcs[name]):
            if isinstance(ret, ast.Return) and ret.value is not None:
                literals |= _returned_literals(ret.value)
        assert literals, f"{name} returns no string literal at all"
        assert literals <= set(declared), (
            f"{name} can return {sorted(literals - set(declared))}, which is "
            f"not in the declared vocabulary {sorted(declared)}. Declare a new "
            "state in app/main.py before returning it."
        )


# ── F17: the total backstop and the gather fallback ────────────────────────


def test_f17_total_backstop_bounds_the_endpoint_when_both_probes_hang(
    client, monkeypatch
):
    """F17 — the outer ``wait_for`` in ``_gather_dependency_checks``.

    Kills: deleting ``asyncio.wait_for(..., _DEPS_PROBE_TOTAL_TIMEOUT_S)``.
    F9 and F9b patch only the PER-PROBE bounds, so both stay green without it.

    ⚠ The body alone does NOT discriminate. With the backstop deleted and both
    probes hanging, the per-probe bounds fire independently and produce the
    SAME ``{"database":"timeout","redis":"timeout"}`` body — just seconds
    later. So the per-probe bounds are pinned HIGH here and the backstop LOW,
    and the kill is wall-clock: the mutant takes ~3s, this takes ~0.05s. The
    body assertions still matter for their own reason — the backstop must
    degrade into the normal shape, never a 500 and never an empty body.
    """
    import time

    from app import main as app_main

    monkeypatch.setattr(app_main, "_DB_PROBE_TIMEOUT_S", 3.0)
    monkeypatch.setattr(app_main, "_REDIS_PROBE_TIMEOUT_S", 3.0)
    monkeypatch.setattr(app_main, "_DEPS_PROBE_TOTAL_TIMEOUT_S", 0.05)
    _break_db(monkeypatch, hang=True)
    _set_redis(monkeypatch, _Pinger(hang=True))
    _set_env(monkeypatch, "production")

    started = time.monotonic()
    r = client.get(DEPS)
    elapsed = time.monotonic() - started

    assert r.status_code == 503, r.text
    assert r.json() == {
        "status": "unhealthy",
        "checks": {"database": "timeout", "redis": "timeout"},
    }
    assert elapsed < 1.0, (
        f"endpoint took {elapsed:.2f}s with the total backstop patched to "
        "0.05s and both per-probe bounds at 3.0s; the backstop is not being "
        "applied, so nothing bounds this endpoint above the per-probe budgets"
    )


def test_f17b_probe_bounds_fit_under_the_backstop(client, monkeypatch):
    """F17b — the shipped constants are consistent with the shipped topology.

    The probes run CONCURRENTLY under one ``gather``, so the floor the
    backstop has to clear is ``max(db, redis)``, not their sum. If a future
    edit raises a per-probe bound above the backstop, that probe's own timeout
    becomes unreachable and every slow dependency reports as BOTH checks
    timing out — silently losing the other probe's real answer, which is the
    exact property F9 exists to protect.
    """
    from app import main as app_main

    assert (
        max(app_main._DB_PROBE_TIMEOUT_S, app_main._REDIS_PROBE_TIMEOUT_S)
        < app_main._DEPS_PROBE_TOTAL_TIMEOUT_S
    ), (
        f"db={app_main._DB_PROBE_TIMEOUT_S}s "
        f"redis={app_main._REDIS_PROBE_TIMEOUT_S}s "
        f"backstop={app_main._DEPS_PROBE_TOTAL_TIMEOUT_S}s"
    )


def test_f17c_a_raising_probe_degrades_instead_of_500ing(client, monkeypatch):
    """F17c — the ``return_exceptions=True`` fallback in the gather.

    Kills: ``raw[1]`` instead of ``raw[1] if isinstance(raw[1], str) else
    "unreachable"``. A probe is supposed to never raise, but if a future edit
    makes one raise, the endpoint an operator reads mid-incident must still
    answer — with the other dependency's real state — rather than 500.

    Without the isinstance guard the exception INSTANCE reaches the response
    body and JSON serialisation fails, which is a 500: a monitoring endpoint
    that goes down with the thing it monitors.
    """
    from app import main as app_main

    async def _exploding_probe() -> str:
        raise RuntimeError("probe bug")

    monkeypatch.setattr(app_main, "_probe_redis", _exploding_probe)
    _set_env(monkeypatch, "production")

    r = client.get(DEPS)

    assert r.status_code == 503, r.text
    assert r.json() == {
        "status": "unhealthy",
        "checks": {"database": "ok", "redis": "unreachable"},
    }


# ── F18-F19: /ready's OWN new code path ────────────────────────────────────


class _LogRecorder:
    """Structlog-shaped recorder bound onto the module's own ``logger``.

    ⚠ Deliberately NOT ``structlog.testing.capture_logs()``. That helper
    installs a processor globally and this repo has been bitten by fences that
    are green alone and on either half of the suite but red in a full run,
    because another module's configuration ran first. Substituting the
    module's own name has no such ordering dependence.
    """

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    def _record(self, level):
        def log(event, **kw):
            self.calls.append((level, event, kw))

        return log

    def __getattr__(self, name):
        return self._record(name)


def test_f18_ready_reports_503_with_its_frozen_body_when_the_database_is_down(
    client, monkeypatch
):
    """F18 — ``/ready``'s failure branch, which nothing drove.

    F10 pins the 200 body; this diff CHANGED ``/ready`` (the query now runs
    under ``asyncio.wait_for``) and its 503 half had no fence at all. The body
    is asserted by strict equality because a rotation gate's contract is what
    ``scripts/smoke-test.sh`` reads.
    """
    _break_db(monkeypatch)
    _set_redis(monkeypatch, _Pinger())
    _set_env(monkeypatch, "production")

    r = client.get("/ready")

    assert r.status_code == 503, r.text
    assert r.json() == {"status": "not_ready", "database": "connection error"}


def test_f19_ready_is_bounded_and_logs_a_discriminating_error(client, monkeypatch):
    """F19 — the ``wait_for`` added to ``/ready`` in this diff.

    Two kills in one, both on the same wedged-socket mode.

    1. Removing the bound. ``_break_db(hang=True)`` never returns, so without
       ``asyncio.wait_for`` this request hangs instead of 503ing — the exact
       state the bound exists for, since aiomysql 0.2.0 accepts no
       ``read_timeout`` and an established-but-wedged socket has no driver
       bound at all. The wall-clock assertion is the discriminator.
    2. Logging ``error=str(e)`` alone. ``wait_for`` raises a BARE
       ``TimeoutError()``, so ``str(e)`` is ``""`` and the one log line an
       operator gets on this failure mode carries no detail whatsoever.
    """
    import time

    from app import main as app_main

    recorder = _LogRecorder()
    monkeypatch.setattr(app_main, "logger", recorder)
    monkeypatch.setattr(app_main, "_DB_PROBE_TIMEOUT_S", 0.05)
    _break_db(monkeypatch, hang=True)
    _set_redis(monkeypatch, _Pinger())
    _set_env(monkeypatch, "production")

    started = time.monotonic()
    r = client.get("/ready")
    elapsed = time.monotonic() - started

    assert r.status_code == 503, r.text
    assert r.json() == {"status": "not_ready", "database": "connection error"}
    assert elapsed < 1.0, (
        f"/ready took {elapsed:.2f}s against a hanging connect with the bound "
        "patched to 0.05s; nothing is bounding the query"
    )

    failures = [c for c in recorder.calls if c[1] == "readiness check failed"]
    assert failures, f"no failure log emitted; recorded: {recorder.calls}"
    kwargs = failures[0][2]
    detail = " ".join(str(v) for v in kwargs.values())
    assert "TimeoutError" in detail, (
        "the readiness failure log carries no discriminating detail. "
        "asyncio.wait_for raises a bare TimeoutError(), so error=str(e) is the "
        f"empty string on precisely this failure mode. Got: {kwargs}"
    )
