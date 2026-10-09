"""Fences for ``GET /health/dependencies`` (TBD-413, INFRA-122).

## What this endpoint exists to stop

On 2026-08-19, during the TBD-360 cutover, the session store (then Valkey)
was enforcing a stale password. Every login returned 503 while ``/ready``
returned ``200 {"status":"ready","database":"connected"}``. Every external
signal said healthy on an app where nobody could log in.

Since INFRA-122 sessions, single-use tokens and leases live in the same MySQL
as the data, so the database is the one required dependency and the body is
``{"status", "checks": {"database"}}``. ``/ready`` is the ROTATION gate and
keeps its response contract byte-identical; this endpoint carries the
per-dependency truth. F10 is the fence that keeps ``/ready`` unchanged.

## Reading these tests

Every test substitutes the database engine in-process (``healthy_db`` and
``_break_db``); none relies on an ambient database.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import pathlib

import pytest
from fastapi.testclient import TestClient

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


def test_f1_database_ok_is_200(client):
    """F1 — the healthy baseline."""
    r = client.get(DEPS)

    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok", "checks": {"database": "ok"}}


def test_f7_database_down_is_503(client, monkeypatch):
    """F7 — a failing database reports ``unreachable`` and 503, with the
    body keys the monitor reads."""
    _break_db(monkeypatch)

    r = client.get(DEPS)

    assert r.status_code == 503, r.text
    assert r.json() == {"status": "unhealthy", "checks": {"database": "unreachable"}}


def test_f9b_hanging_database_times_out(client, monkeypatch):
    """F9b — the same for the database side.

    ⚠ This bound is the ONLY one on the query: per ``database.py:22-31``
    aiomysql 0.2.0 accepts no ``read_timeout``, so ``connect_timeout`` covers
    establishment only and a wedged established socket has no driver bound.
    """
    from app import main as app_main

    monkeypatch.setattr(app_main, "_DB_PROBE_TIMEOUT_S", 0.05)
    _break_db(monkeypatch, hang=True)

    r = client.get(DEPS)

    assert r.status_code == 503, r.text
    body = r.json()
    assert body["checks"]["database"] == "timeout", body


# ── F10-F11: the endpoints that must NOT change ────────────────────────────


def test_f10_ready_is_unchanged(client):
    """F10 — ``/ready`` keeps its contract byte-identical: database only.

    Kills the tempting "helpful" edit of adding dependency checks to the
    rotation gate, which would evict every replica on a shared outage.
    """
    r = client.get("/ready")

    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ready", "database": "connected"}


def test_f11_health_is_pure_liveness_with_everything_down(client, monkeypatch):
    """F11 — ``/health`` must never depend on anything external."""
    _break_db(monkeypatch)
    monkeypatch.setattr(settings, "tbd_app_version", "dev")
    monkeypatch.setattr(settings, "tbd_app_revision", "dev")

    r = client.get("/health")

    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok", "version": "dev", "revision": "dev"}


def test_health_reports_the_baked_version_and_revision(client, monkeypatch):
    """INFRA-42. The post-release smoke test asserts these on the published
    image (release contract section 5). Kills a hardcoded value, and an env
    name that drifts from the ENV the Dockerfile bakes."""
    from app.config import Settings

    monkeypatch.setenv("TBD_APP_VERSION", "1.2.3")
    monkeypatch.setenv("TBD_APP_REVISION", "a" * 40)
    baked = Settings()
    monkeypatch.setattr(settings, "tbd_app_version", baked.tbd_app_version)
    monkeypatch.setattr(settings, "tbd_app_revision", baked.tbd_app_revision)

    r = client.get("/health")

    assert r.json() == {"status": "ok", "version": "1.2.3", "revision": "a" * 40}
    # Baked in the shared `runtime` stage, so both the prod and the
    # migrations image carry it.
    dockerfile = (pathlib.Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    runtime = dockerfile.split("AS runtime\n", 1)[1].split("\nFROM runtime AS migrations", 1)[0]
    assert "\nENV TBD_APP_VERSION=$APP_VERSION" in runtime
    assert "TBD_APP_REVISION=$APP_REVISION" in runtime


# ── F15: no leaks ──────────────────────────────────────────────────────────


def test_f15_response_never_leaks_exception_detail(client, monkeypatch):
    """F15 — this endpoint is unauthenticated. Coarse strings only.

    Kills: ``str(exc)`` in the body, which would publish hostnames, ports,
    driver messages and occasionally credentials to anonymous callers.
    """
    _break_db(monkeypatch, exc=OSError("mysql://root:s3cr3t@10.42.0.5:3306 refused"))

    r = client.get(DEPS)

    raw = r.text
    for leak in ("s3cr3t", "10.42", "3306", "refused"):
        assert leak not in raw, f"response leaked {leak!r}: {raw}"


# ── F16: the vocabulary is CLOSED, and closed at the source ────────────────


def _sweep_every_state(client, monkeypatch) -> tuple[set[str], set[str]]:
    """Drive one request per branch the probe can take.

    Returns ``(statuses, db_states)`` — every value the endpoint actually
    produced. ``monkeypatch.undo()`` between scenarios so each starts from a
    clean slate; the healthy database is re-applied per scenario.
    """
    from app import main as app_main

    statuses: set[str] = set()
    db_states: set[str] = set()

    for db_mode in (None, "db_broken", "db_hang"):
        monkeypatch.undo()
        _set_db_ok(monkeypatch)
        if db_mode == "db_broken":
            _break_db(monkeypatch)
        elif db_mode == "db_hang":
            monkeypatch.setattr(app_main, "_DB_PROBE_TIMEOUT_S", 0.05)
            _break_db(monkeypatch, hang=True)

        body = client.get(DEPS).json()
        statuses.add(body["status"])
        db_states.add(body["checks"]["database"])

    return statuses, db_states


def test_f16_every_produced_state_is_declared_and_every_declaration_is_reachable(
    client, monkeypatch
):
    """F16 — the closed vocabulary, fenced in BOTH directions.

    Drives every branch of the probe and compares the SET of values it
    produces against ``app.main._DB_STATES``, where the contract is declared:

      * a state produced but not declared fails the subset direction — a new
        state must be DECLARED before it can be returned;
      * a state declared but never produced fails the superset direction, so
        the vocabulary cannot drift into documentation of dead branches.
    """
    from app import main as app_main

    statuses, db_states = _sweep_every_state(client, monkeypatch)

    assert statuses == app_main._STATUS_VALUES, (
        f"top-level status values produced {statuses}, declared "
        f"{set(app_main._STATUS_VALUES)}"
    )
    assert db_states == set(app_main._DB_STATES), (
        f"database states produced {db_states}, declared {set(app_main._DB_STATES)}"
    )


def _returned_literals(expr: ast.expr) -> set[str]:
    """The string values a ``return <expr>`` can actually yield.

    ⚠ Deliberately NOT ``ast.walk(return_node)``: that sweeps the whole
    subtree and would report a constant being COMPARED against as a
    returnable state. Only the value positions count.
    """
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return {expr.value}
    if isinstance(expr, ast.IfExp):
        return _returned_literals(expr.body) | _returned_literals(expr.orelse)
    return set()


def test_f16b_probe_returns_no_undeclared_string_literal():
    """F16b — the same closure over the branches F16 does not drive.

    PARSES ``app/main.py`` and collects every string literal the probe can
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
    name, declared = "_probe_database", app_main._DB_STATES
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


# ── F17: the total backstop ────────────────────────────────────────────────


def test_f17_total_backstop_bounds_the_endpoint_when_the_probe_hangs(
    client, monkeypatch
):
    """F17 — the outer ``wait_for`` in ``_gather_dependency_checks``.

    Kills: deleting ``asyncio.wait_for(..., _DEPS_PROBE_TOTAL_TIMEOUT_S)``.
    F9b patches only the PER-PROBE bound, so it stays green without it.

    ⚠ The body alone does NOT discriminate: with the backstop deleted the
    per-probe bound fires and produces the SAME ``timeout`` body, just
    seconds later. So the per-probe bound is pinned HIGH here and the
    backstop LOW, and the kill is wall-clock.
    """
    import time

    from app import main as app_main

    monkeypatch.setattr(app_main, "_DB_PROBE_TIMEOUT_S", 3.0)
    monkeypatch.setattr(app_main, "_DEPS_PROBE_TOTAL_TIMEOUT_S", 0.05)
    _break_db(monkeypatch, hang=True)

    started = time.monotonic()
    r = client.get(DEPS)
    elapsed = time.monotonic() - started

    assert r.status_code == 503, r.text
    assert r.json() == {
        "status": "unhealthy",
        "checks": {"database": "timeout"},
    }
    assert elapsed < 1.0, (
        f"endpoint took {elapsed:.2f}s with the total backstop patched to "
        "0.05s and the per-probe bound at 3.0s; the backstop is not being "
        "applied, so nothing bounds this endpoint above the per-probe budget"
    )


def test_f17b_probe_bound_fits_under_the_backstop():
    """F17b — the shipped constants are consistent: if a future edit raises
    the per-probe bound above the backstop, the probe's own timeout becomes
    unreachable and every slow database reports through the backstop."""
    from app import main as app_main

    assert app_main._DB_PROBE_TIMEOUT_S < app_main._DEPS_PROBE_TOTAL_TIMEOUT_S, (
        f"db={app_main._DB_PROBE_TIMEOUT_S}s "
        f"backstop={app_main._DEPS_PROBE_TOTAL_TIMEOUT_S}s"
    )


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
