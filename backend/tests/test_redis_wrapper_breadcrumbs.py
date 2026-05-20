"""Wrapper-layer breadcrumbs for the auth Redis call path.

Gated by ``settings.auth_debug_logging`` so production stays quiet
under normal operation. When the flag is flipped on during incident
triage, every wrapped Redis op emits ``redis.call.start`` and either
``redis.call.ok`` or ``redis.call.error`` with the function name and
duration — that data was the missing piece on the 2026-05-20 silent
46 s hang where uvicorn never emitted an access log because the
handler hung mid-await.

These tests patch ``redis_client.logger`` directly and inspect call
args because the logger is a structlog ``BoundLogger`` whose kwargs
do not appear as plain attributes on the underlying ``LogRecord``
that ``caplog`` captures — the contract surface is what gets passed
to ``logger.info`` / ``logger.warning``, not the post-format record
shape.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError as RedisResponseError

from app.redis_client import _normalize_transport_errors


class TestBreadcrumbsGated:
    @pytest.mark.asyncio
    async def test_breadcrumbs_silent_when_flag_off(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Default-off: zero ``redis.call.*`` events emitted regardless
        of success or failure. Production noise floor stays untouched."""
        import app.redis_client as rc

        monkeypatch.setattr(rc.settings, "auth_debug_logging", False)

        @_normalize_transport_errors
        async def helper() -> int:
            return 1

        with patch.object(rc, "logger") as logger_mock:
            result = await helper()
        assert result == 1
        assert logger_mock.info.call_args_list == [], (
            "expected no breadcrumbs with flag off; got "
            f"{logger_mock.info.call_args_list}"
        )

    @pytest.mark.asyncio
    async def test_breadcrumbs_on_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Flag-on success path emits start + ok with op name and
        duration. The wrapped function's ``__name__`` is the
        operator-visible op label — preserved by ``functools.wraps``."""
        import app.redis_client as rc

        monkeypatch.setattr(rc.settings, "auth_debug_logging", True)

        @_normalize_transport_errors
        async def session_validate() -> str:
            return "ok"

        with patch.object(rc, "logger") as logger_mock:
            await session_validate()

        info_calls = logger_mock.info.call_args_list
        assert [c.args[0] for c in info_calls] == [
            "redis.call.start",
            "redis.call.ok",
        ]
        # Start event carries op name only (no duration — call hasn't
        # run yet); ok event carries op name + duration_ms.
        assert info_calls[0].kwargs == {"op": "session_validate"}
        ok_kwargs = info_calls[1].kwargs
        assert ok_kwargs["op"] == "session_validate"
        assert isinstance(ok_kwargs.get("duration_ms"), (int, float))

    @pytest.mark.asyncio
    async def test_breadcrumbs_on_redis_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """RedisError still propagates unchanged AND the wrapper emits a
        ``redis.call.error`` breadcrumb carrying the error class so the
        operator can grep for transient ConnectionError bursts vs Lua
        ResponseError without spelunking tracebacks."""
        import app.redis_client as rc

        monkeypatch.setattr(rc.settings, "auth_debug_logging", True)

        @_normalize_transport_errors
        async def session_rotate_lua() -> None:
            raise RedisResponseError("session_revoked")

        with patch.object(rc, "logger") as logger_mock:
            with pytest.raises(RedisResponseError):
                await session_rotate_lua()

        info_calls = logger_mock.info.call_args_list
        assert [c.args[0] for c in info_calls] == [
            "redis.call.start",
            "redis.call.error",
        ]
        error_kwargs = info_calls[1].kwargs
        assert error_kwargs["op"] == "session_rotate_lua"
        assert error_kwargs["error_class"] == "ResponseError"
        assert isinstance(error_kwargs.get("duration_ms"), (int, float))

    @pytest.mark.asyncio
    async def test_breadcrumbs_on_oserror_translation(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """OSError is translated to RedisConnectionError by the wrapper.
        The breadcrumb on the error path must carry the ORIGINAL error
        class name (``BrokenPipeError``) so the operator can tell the
        transport-failure class apart from a normal RedisConnectionError
        that some other layer raised."""
        import app.redis_client as rc
        from unittest.mock import AsyncMock, MagicMock

        monkeypatch.setattr(rc.settings, "auth_debug_logging", True)

        # Retirement path needs a sentinel client to drop; install one
        # so the wrapper completes its full path.
        sentinel_client = MagicMock()
        sentinel_client.aclose = AsyncMock()
        rc._client = sentinel_client

        @_normalize_transport_errors
        async def session_validate() -> None:
            raise BrokenPipeError(32, "Broken pipe")

        with patch.object(rc, "logger") as logger_mock:
            with pytest.raises(RedisConnectionError):
                await session_validate()

        info_calls = logger_mock.info.call_args_list
        assert [c.args[0] for c in info_calls] == [
            "redis.call.start",
            "redis.call.error",
        ]
        assert info_calls[1].kwargs["error_class"] == "BrokenPipeError"
