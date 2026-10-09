import functools
import logging
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import bcrypt
import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# The app settings module validates JWT_SECRET_KEY at import time.
# Tests set a stable secret up front so importing app modules does not
# depend on an external .env file being present in the worktree.
os.environ.setdefault(
    "JWT_SECRET_KEY",
    "test-jwt-secret-that-is-long-enough-for-pytest-1234567890",
)
os.environ.setdefault("APP_ENV", "development")

# INFRA-105: tests never export telemetry, whatever the shell sets. configure() must run before
# app.main is imported (it builds FastAPI() at import, which looks the MeterProvider up then).
for _otel in [name for name in os.environ if name.startswith("OTEL_")]:
    del os.environ[_otel]

from opentelemetry import metrics as _otel_metrics  # noqa: E402
from opentelemetry import trace as _otel_trace  # noqa: E402
from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: E402
from opentelemetry.sdk.trace import SpanProcessor  # noqa: E402

from app import tracing as _tracing  # noqa: E402

_tracing.configure("tbd-api")


class _SpanRecorder(SpanProcessor):
    """Keeps finished spans only while the ``spans`` fixture is active: an always-on in-memory
    exporter would hold every span of the session in each xdist worker."""

    finished: list | None = None

    def on_end(self, span) -> None:
        if self.finished is not None:
            self.finished.append(span)


_SPAN_RECORDER = _SpanRecorder()
_otel_trace.get_tracer_provider().add_span_processor(_SPAN_RECORDER)


@pytest.fixture
def spans():
    """Finished spans recorded since this fixture started, oldest first."""
    _SPAN_RECORDER.finished = []
    yield lambda: list(_SPAN_RECORDER.finished)
    _SPAN_RECORDER.finished = None


@pytest.fixture
def metric_reader():
    """A reader on the global MeterProvider: sees only what is recorded after it was added."""
    provider = _otel_metrics.get_meter_provider()
    reader = InMemoryMetricReader()
    provider.add_metric_reader(reader)
    yield reader
    provider.remove_metric_reader(reader)

# INFRA-51: bcrypt cost 12 (the production default) makes every hash_password
# call in a fixture cost ~250ms; 149 test files do it. Use cost 4 (bcrypt's
# minimum) in tests only. Production hashing is untouched: app/security.py
# still calls bcrypt.gensalt() with no rounds, and test_security.py proves it
# by restoring the original via ``bcrypt.gensalt.__wrapped__``.
# getattr: stay idempotent if this module is ever imported twice.
_real_gensalt = getattr(bcrypt.gensalt, "__wrapped__", bcrypt.gensalt)


@functools.wraps(_real_gensalt)
def _cheap_gensalt(rounds: int = 4, prefix: bytes = b"2b") -> bytes:
    return _real_gensalt(rounds=rounds, prefix=prefix)


bcrypt.gensalt = _cheap_gensalt

# Match the production logging.py suppression: ofxtools emits per-row INFO
# during OFX parses ("Converting <STMTTRN>"). For tests that parse the
# 10k-row fixture this distorts wall-clock timing AND floods captured
# log output. Apply the same WARNING floor at conftest import so it
# takes effect before any test session-level fixture imports parser
# modules.
logging.getLogger("ofxtools").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Full-suite collection capture, for the .test_durations freshness fence.
#
# ⚠ MUST run BEFORE pytest-split deselects. Under `--splits N --group G`,
# `session.items` at test time holds only ~1/N of the suite, and it is a
# NON-RANDOM 1/N: pytest-split places recorded tests deterministically, so a
# coverage ratio measured over one shard is biased TOWARD looking healthy.
#
# `PytestSplitPlugin.pytest_collection_modifyitems` is declared
# `@hookimpl(trylast=True)` and DESELECTS rather than reducing collection, so a
# `tryfirst` implementation here observes the whole suite in every shard.
#
# Changing `tryfirst` to `trylast`, or returning early, makes
# test_test_durations_freshness.py measure coverage against a single shard and
# report ~100% while 5/6 of the suite is unmodelled. The fence's
# `MIN_COLLECTED` floor exists to turn exactly that mutation RED.
# ---------------------------------------------------------------------------
# ⚠⚠ THIS IMPORT MUST STAY BELOW THE sys.path.insert ABOVE. `backend/tests/`
# has no __init__.py and CI sets no PYTHONPATH, so `tests.` only resolves
# because BACKEND_ROOT was put on sys.path at the top of this file. An
# autoformatter or isort pass that hoists this to the import block makes
# EVERY CI run die at conftest load with `ModuleNotFoundError: No module
# named 'tests'`. That is what the noqa: E402 is protecting.
#
# ⚠ The set lives in its own module, NOT here: this conftest is imported
# twice under two names (`conftest` and `tests.conftest`) and a module-level
# set here would exist as two distinct objects. See _durations_registry.
from tests._durations_registry import (  # noqa: E402
    COLLECTED_NODEIDS,
    COLLECTED_ORDER,
)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    COLLECTED_NODEIDS.update(item.nodeid for item in items)
    if not COLLECTED_ORDER:
        COLLECTED_ORDER.extend(item.nodeid for item in items)


def set_refresh_cookie(client: Any, token: str) -> None:
    """Pin ``refresh_token`` on the test client's cookie jar.

    Replaces the deprecated per-request ``cookies={"refresh_token": ...}``
    kwarg (Starlette/httpx warn that per-request cookie persistence is
    ambiguous). We first ``delete`` every existing ``refresh_token`` entry
    across all domains/paths — including any canonical or legacy-path cookie
    the server set on a prior rotation — then ``set`` exactly the intended
    value, so the next request sends only this token. This reproduces the
    per-request override behaviour deterministically (no duplicate-cookie
    ``CookieConflict``). Works for both ``TestClient`` and
    ``httpx.AsyncClient`` (both expose an ``httpx.Cookies`` jar).
    """
    client.cookies.delete("refresh_token")
    client.cookies.set("refresh_token", token)


@pytest.fixture(autouse=True)
def _autouse_clear_structlog_contextvars():
    """Give every test an empty structlog context, before AND after.

    In production ``RequestContextMiddleware`` (pure ASGI) calls
    ``clear_contextvars()`` at the top of every HTTP request, so nothing
    bleeds between requests. Test apps built as a bare ``FastAPI()`` do not
    mount that middleware, and a handful of sync tests bind on the main
    thread directly (``test_log_field_propagation.py``,
    ``test_request_context.py``) and clear only by convention.

    That mattered little while contextvars were log decoration. Since
    TBD-188 ``audit_service._build_audit_event`` READS ``api_token_id`` from
    this context, so a leftover bind from an earlier test could stamp a real
    token id onto an unrelated test's audit row. This fixture makes the empty
    starting context a property of the harness rather than of test ordering.

    Deliberately a fixture and NOT a test: asserting on cross-test contextvar
    hygiene requires a specific execution order, and an order-dependent test
    is worse than the defect it guards against.
    """
    import structlog

    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


@pytest.fixture(autouse=True)
def _autouse_enable_auth_debug_logging(monkeypatch):
    """Enable the ``auth.refresh.rejected`` structured event in every
    test by default.

    Production ``settings.auth_debug_logging`` defaults to ``False`` so
    INFO logs stay quiet under normal operation (operators flip it on
    during incident triage). The test suite needs the events to fire
    so it can assert on them; the few tests covering the OFF behaviour
    explicitly override this fixture with their own ``monkeypatch``.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "auth_debug_logging", True)


@pytest.fixture(autouse=True)
def _autouse_disable_scheduler(monkeypatch):
    """Disable the background scheduler for every test by default.

    Task 12 wired ``scheduler_loop`` into the FastAPI ``lifespan`` (see
    ``app/main.py``), started whenever ``app_settings.scheduler_enabled``
    is True (the production default). Any test that boots the real app
    with its lifespan (e.g. ``with TestClient(app_main.app)``) would
    otherwise start a live scheduler that queries orgs and can COMMIT
    recurring-generation / billing-close writes concurrently with the
    test — nondeterministic, suite-wide.

    ``app/main.py`` imports the settings singleton as
    ``from app.config import settings as app_settings``, so patching
    ``app.config.settings`` here is patching the exact object the
    lifespan reads. The dedicated
    ``tests/test_scheduler_lifespan.py`` test re-enables the scheduler
    itself (inside the test body, after fixtures run), so its own
    ``monkeypatch.setattr`` on the same attribute takes precedence there.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "scheduler_enabled", False)


def issue_test_refresh_token(user_id: int, **kwargs) -> str:
    """Test helper: mint a refresh JWT AND seed its session family row, so
    the validation chain accepts the token in subsequent requests. Tests that
    hand-mint a refresh JWT to bypass /login use this rather than
    ``create_refresh_token`` directly."""
    from app import state_db
    from app.security import create_refresh_token, default_session_ttl_seconds

    token, jti, sid = create_refresh_token(user_id, **kwargs)
    state_db._issue(jti, sid, user_id, kwargs.get("ttl_seconds") or default_session_ttl_seconds())
    return token


# ---------------------------------------------------------------------------
# Fast ``Base.metadata.create_all`` for empty SQLite DBs (INFRA-52).
#
# ~280 test call sites run ``conn.run_sync(Base.metadata.create_all)`` on a
# fresh in-memory DB; SQLAlchemy re-compiles the DDL and issues a checkfirst
# PRAGMA per table each time (~20% of a shard). Compile once per session,
# replay the raw DDL per call. Anything else (non-SQLite, non-empty DB,
# ``tables=``/``checkfirst=`` args, Engine instead of Connection, other
# MetaData objects) falls through to the real create_all, so custom or
# partial schemas are untouched. Each call still runs on its own connection,
# so per-test isolation is unchanged.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def _fast_sqlite_create_all():
    from sqlalchemy import create_engine
    from sqlalchemy.engine import Connection

    from app.models import Base

    md = Base.metadata
    real = md.create_all
    cache: dict[tuple, list[str]] = {}

    def _ddl() -> list[str]:
        key = tuple(sorted(md.tables))  # rebuild if a test adds tables
        if key not in cache:
            eng = create_engine("sqlite://")
            real(eng)
            with eng.connect() as c:
                cache[key] = [
                    r[0]
                    for r in c.exec_driver_sql(
                        "SELECT sql FROM sqlite_master "
                        "WHERE sql IS NOT NULL ORDER BY rowid"
                    )
                ]
            eng.dispose()
        return cache[key]

    def fast(bind, *args, **kwargs):
        if (
            isinstance(bind, Connection)
            and bind.dialect.name == "sqlite"
            and not args
            and not kwargs
            and bind.exec_driver_sql("SELECT 1 FROM sqlite_master LIMIT 1").first() is None
        ):
            for stmt in _ddl():
                bind.exec_driver_sql(stmt)
            return None
        return real(bind, *args, **kwargs)

    md.create_all = fast
    yield
    del md.create_all


# ---------------------------------------------------------------------------
# Rate limits move to MySQL (INFRA-121). Each xdist worker gets its own SQLite
# FILE (QueuePool: every to_thread worker thread has its own connection; a
# StaticPool would interleave transactions across threads).
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def _rate_limit_engine(tmp_path_factory):
    from sqlalchemy import create_engine, event

    from app import rate_limit_db
    from app.models.rate_limit import RateLimit

    eng = create_engine(f"sqlite:///{tmp_path_factory.mktemp('rl')}/rl.db")

    @event.listens_for(eng, "connect")
    def _fast(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA synchronous=OFF")
        cur.execute("PRAGMA journal_mode=MEMORY")
        cur.close()

    RateLimit.__table__.create(eng)
    real = rate_limit_db._engine
    rate_limit_db._engine = eng
    yield eng
    rate_limit_db._engine = real
    eng.dispose()


@pytest.fixture(autouse=True)
def _clear_rate_limits(_rate_limit_engine):
    from sqlalchemy import text

    with _rate_limit_engine.begin() as c:
        c.execute(text("DELETE FROM rate_limits"))


# ---------------------------------------------------------------------------
# Sessions, single-use tokens and leases move to MySQL (INFRA-122). Own
# per-worker SQLite file, swapped into ``state_db._engine``; cleared per test.
# ---------------------------------------------------------------------------
_STATE_TABLES = ("auth_session_members", "auth_session_families", "used_tokens", "leases")


@pytest.fixture(scope="session", autouse=True)
def _state_engine(tmp_path_factory):
    """STATE_DB_TEST_URL=mysql+aiomysql://... runs every test on that MySQL
    instead (single process, ``-p no:xdist``; a disposable database)."""
    from sqlalchemy import event

    from app import rate_limit_db, state_db
    from app.models import Base

    url = os.environ.get("STATE_DB_TEST_URL")
    if url:
        eng = rate_limit_db._build_engine(url)
        assert eng.dialect.name == "mysql", "STATE_DB_TEST_URL must be a MySQL URL"
    else:
        eng = rate_limit_db._build_engine(f"sqlite:///{tmp_path_factory.mktemp('state')}/state.db")

        @event.listens_for(eng, "connect")
        def _fast(dbapi_conn, _rec):
            dbapi_conn.isolation_level = None  # we emit BEGIN ourselves
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA synchronous=OFF")
            cur.execute("PRAGMA journal_mode=MEMORY")
            cur.close()

        @event.listens_for(eng, "begin")
        def _immediate(conn):
            # SQLite ignores FOR UPDATE: take the write lock up front so
            # concurrent session writes in tests serialize as the row lock does on MySQL.
            conn.exec_driver_sql("BEGIN IMMEDIATE")

    Base.metadata.create_all(eng, tables=[Base.metadata.tables[t] for t in _STATE_TABLES], checkfirst=True)
    real = state_db._engine
    state_db._engine = eng
    yield eng
    state_db._engine = real
    eng.dispose()


@pytest.fixture(autouse=True)
def _clear_state(_state_engine):
    from sqlalchemy import text

    with _state_engine.begin() as c:
        for t in _STATE_TABLES:
            c.execute(text(f"DELETE FROM {t}"))


@pytest.fixture
def state_db_down():
    """The session/token/lease store refuses connections."""
    from sqlalchemy import create_engine

    from app import state_db

    saved = state_db._engine
    state_db._engine = create_engine("sqlite:////nonexistent-dir/state.db")
    try:
        yield
    finally:
        state_db._engine.dispose()
        state_db._engine = saved


def state_family(sid: str) -> dict | None:
    """The family row as a dict (test helper)."""
    from sqlalchemy import select

    from app import state_db

    with state_db._engine.connect() as c:
        row = c.execute(select(state_db._F).where(state_db._F.c.sid == sid)).mappings().first()
    return dict(row) if row else None


def state_jtis(sid: str) -> set[str]:
    """Every member jti of a family (test helper)."""
    from sqlalchemy import select

    from app import state_db

    with state_db._engine.connect() as c:
        return set(c.execute(select(state_db._M.c.jti).where(state_db._M.c.sid == sid)).scalars())


def expire_grace(sid: str) -> None:
    """Age every member of the family past the rotation grace window, so no
    rotated-out jti is graced any more (test time travel)."""
    from sqlalchemy import update

    from app import state_db

    with state_db._engine.begin() as c:
        c.execute(
            update(state_db._M)
            .where(state_db._M.c.sid == sid)
            .values(created_at=state_db.db_now(-state_db.SESSION_GRACE_TTL_SECONDS - 1))
        )


def seconds_until(column, *where) -> float:
    """Seconds from the DB clock to a state_db time column (SQLite or MySQL)."""
    from sqlalchemy import func, select, text

    from app import state_db

    with state_db._engine.connect() as c:
        if c.dialect.name == "sqlite":
            q = select((func.julianday(column) - func.julianday(state_db.db_now())) * 86400)
        else:
            q = select(func.timestampdiff(text("SECOND"), state_db.db_now(), column))
        return float(c.execute(q.where(*where)).scalar())


def expire_family(sid: str) -> None:
    from sqlalchemy import update

    from app import state_db

    with state_db._engine.begin() as c:
        c.execute(update(state_db._F).where(state_db._F.c.sid == sid).values(expires_at=state_db.db_now(-1)))


def expire_lease(name: str) -> None:
    from sqlalchemy import update

    from app import state_db

    with state_db._engine.begin() as c:
        c.execute(update(state_db._L).where(state_db._L.c.name == name).values(expires_at=state_db.db_now(-1)))


def expire_token(scope: str, token: str) -> None:
    from sqlalchemy import update

    from app import state_db

    with state_db._engine.begin() as c:
        c.execute(
            update(state_db._U)
            .where(state_db._U.c.scope == scope, state_db._U.c.token == state_db._token_hash(token))
            .values(expires_at=state_db.db_now(-1))
        )


@pytest.fixture
def limits_db_down():
    """The limits DB refuses connections. Restores the engine itself: module
    autouse ``limiter.reset()`` teardowns run before monkeypatch undo."""
    from sqlalchemy import create_engine

    from app import rate_limit_db

    saved = rate_limit_db._engine
    rate_limit_db._engine = create_engine("sqlite:////nonexistent-dir/x.db")
    try:
        yield
    finally:
        rate_limit_db._engine.dispose()
        rate_limit_db._engine = saved


@pytest.fixture
def limits_hit_down(monkeypatch):
    """``hit`` fails while ``get`` works (MCP has no slowapi decorator)."""
    from sqlalchemy.exc import OperationalError

    from app import rate_limit_db

    def boom(*_a, **_k):
        raise OperationalError("INSERT", {}, Exception("limits db down"))

    monkeypatch.setattr(rate_limit_db, "hit", boom)
