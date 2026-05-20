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


def test_connect_timeout_is_propagated_to_aiomysql() -> None:
    """``connect_timeout`` is the only socket-level timeout aiomysql
    0.2.0 accepts. The builder must thread the configured value
    through verbatim — a drop here would re-introduce the unbounded
    cold-start connect class. (``read_timeout`` / ``write_timeout``
    are deliberately NOT passed because aiomysql 0.2.0 raises
    ``TypeError`` on them; the stale-socket class is bounded at
    ``pool_recycle`` + the route-local handler timeout instead.)"""
    args = database._build_connect_args()
    assert args.get("connect_timeout") == settings.db_connect_timeout
    # Pin the negative contract too: a future bump to aiomysql 0.2.1+
    # might add support for these, but until requirements.txt moves,
    # passing them through here is a hard production failure.
    assert "read_timeout" not in args
    assert "write_timeout" not in args


def test_connect_timeout_has_sane_default() -> None:
    """Default must be bounded (>0 and below the route-local handler
    ceiling) so a stuck initial connect cannot consume the whole
    budget."""
    assert 0 < settings.db_connect_timeout < settings.refresh_handler_timeout_s


def test_connect_args_only_uses_aiomysql_supported_kwargs() -> None:
    """Hard guard against the 2026-05-20 14:22 UTC production incident:
    PR #321's first commit added ``read_timeout`` and ``write_timeout``
    to ``connect_args``, but aiomysql 0.2.0 (the version pinned in
    requirements.txt) does NOT accept them — every DB-touching
    request 500'd with ``TypeError: connect() got an unexpected
    keyword argument 'read_timeout'``. CI tests passed because the
    args dict construction was tested in isolation; aiomysql was
    never asked to accept it. This test closes that gap by
    introspecting aiomysql's real ``connect()`` signature and
    asserting every key we pass is in it."""
    import inspect
    import aiomysql

    supported = set(inspect.signature(aiomysql.connect).parameters)
    args = database._build_connect_args()
    unsupported = set(args) - supported
    assert unsupported == set(), (
        f"connect_args contains kwargs not accepted by aiomysql.connect() "
        f"in the pinned version: {unsupported}. Either drop them or bump "
        f"aiomysql in requirements.txt."
    )


# NOTE: a fourth test originally asserted
# ``database.engine.pool._recycle == settings.db_pool_recycle`` but was
# dropped — the equivalent ``pool_recycle`` kwarg assertion in
# ``test_database_pool_config.test_engine_is_constructed_with_settings_pool_values``
# covers the construction contract without depending on the module-level
# engine reference, which other tests in the suite stub via
# ``importlib.reload`` and a fake ``create_async_engine``.
