"""Engine-level pool / socket-timeout config — regression tests for
the 2026-05-20 silent 46 s /auth/refresh hang.

Without these bounds the SQLAlchemy pool's ``pre_ping`` could block on
a half-open socket until the kernel TCP RTO (tens of seconds), with
no application-level recovery point. Every endpoint that touches the
DB inherited the hang; uvicorn never emitted an access log because the
handler never reached the response stage.
"""

from __future__ import annotations

import pytest

import app.database as database
from app.config import settings


def test_pool_recycle_is_under_typical_nat_idle() -> None:
    """``pool_recycle`` must stay well under the VPC NAT's idle-drop
    interval (~300 s typical). The setting carries the value into the
    engine; this test pins the default so a future bump to e.g. 1800 s
    (the pre-2026-05-20 value) re-introduces the silent-hang class
    immediately and fails CI."""
    assert settings.db_pool_recycle <= 290, (
        "db_pool_recycle must stay under the typical VPC NAT idle "
        f"timeout; got {settings.db_pool_recycle}s"
    )


def test_socket_timeouts_are_propagated_to_aiomysql() -> None:
    """``connect_timeout`` / ``read_timeout`` / ``write_timeout`` are
    aiomysql constructor args that bound socket I/O. Without them, a
    stale pooled connection blocks until the kernel TCP RTO. The
    builder must thread the configured values through verbatim — a
    drop here would silently re-open the hang."""
    args = database._build_connect_args()
    assert args.get("connect_timeout") == settings.db_connect_timeout
    assert args.get("read_timeout") == settings.db_read_timeout
    assert args.get("write_timeout") == settings.db_write_timeout


def test_socket_timeouts_have_sane_defaults() -> None:
    """Defaults must be bounded (>0 and below the route-local handler
    ceiling) so a single stuck operation cannot consume the whole
    budget. read/write timeouts apply per-packet so the values can
    safely be larger than the connect timeout — but never unbounded."""
    assert 0 < settings.db_connect_timeout < settings.refresh_handler_timeout_s
    assert 0 < settings.db_read_timeout < 120
    assert 0 < settings.db_write_timeout < 120


# NOTE: a fourth test originally asserted
# ``database.engine.pool._recycle == settings.db_pool_recycle`` but was
# dropped — the equivalent ``pool_recycle`` kwarg assertion in
# ``test_database_pool_config.test_engine_is_constructed_with_settings_pool_values``
# covers the construction contract without depending on the module-level
# engine reference, which other tests in the suite stub via
# ``importlib.reload`` and a fake ``create_async_engine``.
