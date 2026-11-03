"""K8S-3: SQLAlchemy pool size + max_overflow are explicit and env-overridable.

The L0.6 HPA-readiness audit (2026-05-08) flagged the implicit pool defaults
as a multi-replica risk: under HPA each replica gets its own pool and the
managed-DB max_connections cap can be exceeded silently. These tests pin:

  1. `app.database.engine` is constructed with the pool_size + max_overflow
     values resolved from `app.config.settings` (not SQLAlchemy defaults).
  2. The Settings model exposes `db_pool_size` (default 5) and
     `db_max_overflow` (default 10), both env-overridable through pydantic-
     settings' standard uppercase env-var mapping.
  3. The diagnostic `db.engine.configured` breadcrumb is emitted at module
     import so an operator can grep the deploy log for the live pool config
     rather than reverse-engineer env-var resolution.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

BACKEND = Path(__file__).resolve().parents[1]


def test_settings_expose_pool_defaults():
    """Defaults: 5 / 10. Locked so multi-replica sizing math stays predictable."""
    from app.config import Settings

    s = Settings()
    assert s.db_pool_size == 5
    assert s.db_max_overflow == 10


def test_settings_pool_values_are_env_overridable(monkeypatch):
    """pydantic-settings reads DB_POOL_SIZE / DB_MAX_OVERFLOW from the env."""
    monkeypatch.setenv("DB_POOL_SIZE", "20")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "40")

    from app.config import Settings

    s = Settings()
    assert s.db_pool_size == 20
    assert s.db_max_overflow == 40


def test_engine_is_constructed_with_settings_pool_values(monkeypatch):
    """`app.database` passes settings.db_pool_size + db_max_overflow to
    create_async_engine.

    Reimports `app.database` with create_async_engine patched at the
    source module (``sqlalchemy.ext.asyncio``) so the patch survives
    ``importlib.reload``. Mirrors how Settings is consumed at module-
    import time in production.
    """
    captured: dict = {}

    def _fake_create_async_engine(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs

        class _Dummy:
            def dispose(self):
                pass

        return _Dummy()

    monkeypatch.setenv("DB_POOL_SIZE", "7")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "13")

    import sqlalchemy.ext.asyncio as sa_async

    import app.config as app_config
    import app.database as app_database

    # Capture the original settings singleton so we can restore it after the
    # reload — a second reload creates yet another Settings object and leaves
    # all already-imported ``from app.config import settings`` references
    # stranded on a stale instance (causing cross-test monkeypatch pollution).
    _original_settings = app_config.settings
    # Reloading again would swap in a new engine and new functions, which the
    # conftest dependency overrides no longer match; restore the namespace instead.
    _original_database = dict(vars(app_database))

    monkeypatch.setattr(sa_async, "create_async_engine", _fake_create_async_engine)

    try:
        importlib.reload(app_config)
        importlib.reload(app_database)

        assert captured["kwargs"]["pool_size"] == 7
        assert captured["kwargs"]["max_overflow"] == 13
        # Sanity: existing safety params still on the engine.
        assert captured["kwargs"]["pool_pre_ping"] is True
        # pool_recycle is now sourced from settings.db_pool_recycle —
        # see test_db_engine_config.py for the NAT-idle-ceiling rationale.
        assert captured["kwargs"]["pool_recycle"] == app_config.settings.db_pool_recycle
    finally:
        # Restore the original singleton so ``from app.config import settings``
        # references in other modules continue to see the live object.
        app_config.settings = _original_settings
        vars(app_database).update(_original_database)


def test_engine_configured_breadcrumb_emitted_at_import(monkeypatch, log_lines):
    """`db.engine.configured` is logged at module import with pool params + host.

    The host field must be the URL hostname only, never the credentials.
    """
    try:
        monkeypatch.setenv(
            "DATABASE_URL",
            "mysql+aiomysql://user:secret@db.internal:3306/pfv",
        )
        monkeypatch.setenv("DB_POOL_SIZE", "9")
        monkeypatch.setenv("DB_MAX_OVERFLOW", "11")

        # Stub create_async_engine at the source module so the patch
        # survives ``importlib.reload(app_database)``.
        def _fake_engine(url, **kw):
            class _Dummy:
                def dispose(self):
                    pass
            return _Dummy()

        import sqlalchemy.ext.asyncio as sa_async
        import app.config as app_config
        import app.database as app_database

        # Capture original singleton before reload (see test above for rationale).
        _original_settings = app_config.settings
        _original_database = dict(vars(app_database))

        monkeypatch.setattr(sa_async, "create_async_engine", _fake_engine)
        importlib.reload(app_config)
        importlib.reload(app_database)

        events = [e for e in log_lines() if e["msg"] == "db.engine.configured"]
        assert events, "expected a db.engine.configured event at module import"
        evt = events[-1]
        assert evt["pool_size"] == 9
        assert evt["max_overflow"] == 11
        assert evt["host"] == "db.internal"
        # Credentials must never appear anywhere in the event payload.
        flat = repr(evt)
        assert "secret" not in flat
        assert "user:" not in flat
    finally:
        # Restore original singleton and re-wire app.database to it.
        import app.config as app_config
        import app.database as app_database
        app_config.settings = _original_settings
        vars(app_database).update(_original_database)


# ── TBD-785: the rate-limit and state-db side pools ───────────────────────

# Prints what each side engine's pool and the state_db executor were built with.
_PROBE = (
    "from app import rate_limit_db, state_db\n"
    "r, s = rate_limit_db._engine.pool, state_db._engine.pool\n"
    "print(r.size(), r._max_overflow, s.size(), s._max_overflow, state_db._POOL._max_workers)"
)


def _side_pools(**env: str) -> list[int]:
    # A fresh interpreter: the engines are built at import, and conftest swaps them.
    # A MySQL URL so the QueuePool sizing applies; nothing connects at build time.
    base = {k: v for k, v in os.environ.items() if not k.startswith(("RATE_LIMIT_DB_", "STATE_DB_"))}
    full = {**base, "DATABASE_URL": "mysql+aiomysql://u:p@db.invalid:3306/x", **env}
    r = subprocess.run([sys.executable, "-c", _PROBE], cwd=BACKEND,
                       capture_output=True, text=True, timeout=60, env=full)
    assert r.returncode == 0, r.stderr
    return [int(x) for x in r.stdout.split()]


def test_side_pool_defaults_are_unchanged():
    """FENCE. Wrong implementation: a default drift changes the 29-connection budget."""
    assert _side_pools() == [2, 5, 2, 5, 7]


def test_side_pool_env_overrides_reach_the_engines_and_executor():
    """FENCE. Wrong implementation: a hardcoded size, or one engine reading the other's vars."""
    assert _side_pools(
        RATE_LIMIT_DB_POOL_SIZE="3", RATE_LIMIT_DB_MAX_OVERFLOW="1",
        STATE_DB_POOL_SIZE="4", STATE_DB_MAX_OVERFLOW="0",
    ) == [3, 1, 4, 0, 4]


@pytest.mark.parametrize(("var", "bad", "ok"), [
    ("RATE_LIMIT_DB_POOL_SIZE", "0", "1"),
    ("STATE_DB_POOL_SIZE", "0", "1"),
    ("RATE_LIMIT_DB_MAX_OVERFLOW", "-1", "0"),
    ("STATE_DB_MAX_OVERFLOW", "-1", "0"),
])
def test_side_pool_bounds(monkeypatch, var, bad, ok):
    """FENCE. Wrong implementation: no bound, or an off-by-one refusing the minimum."""
    from app.config import Settings

    monkeypatch.setenv(var, bad)
    with pytest.raises(ValidationError, match=var):
        Settings()
    monkeypatch.setenv(var, ok)
    assert getattr(Settings(), var.lower()) == int(ok)
