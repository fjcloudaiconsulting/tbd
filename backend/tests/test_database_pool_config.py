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

import structlog
from structlog.testing import LogCapture


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

    monkeypatch.setattr(sa_async, "create_async_engine", _fake_create_async_engine)

    try:
        importlib.reload(app_config)
        importlib.reload(app_database)

        assert captured["kwargs"]["pool_size"] == 7
        assert captured["kwargs"]["max_overflow"] == 13
        # Sanity: existing safety params still on the engine.
        assert captured["kwargs"]["pool_pre_ping"] is True
        assert captured["kwargs"]["pool_recycle"] == 1800
    finally:
        # Restore module state for any later tests in the run.
        importlib.reload(app_config)
        importlib.reload(app_database)


def test_engine_configured_breadcrumb_emitted_at_import(monkeypatch):
    """`db.engine.configured` is logged at module import with pool params + host.

    The host field must be the URL hostname only, never the credentials.
    """
    capture = LogCapture()
    structlog.configure(
        processors=[capture],
        wrapper_class=structlog.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )

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

        monkeypatch.setattr(sa_async, "create_async_engine", _fake_engine)
        importlib.reload(app_config)
        importlib.reload(app_database)

        events = [e for e in capture.entries if e.get("event") == "db.engine.configured"]
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
        structlog.reset_defaults()
        # Restore for downstream tests.
        import app.config as app_config
        import app.database as app_database
        importlib.reload(app_config)
        importlib.reload(app_database)
