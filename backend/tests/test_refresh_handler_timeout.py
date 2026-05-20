"""Route-local ``asyncio.wait_for`` on ``/auth/refresh``.

The 2026-05-20 trace showed the handler hanging silently — no
``uvicorn.access`` entry, no structured ``auth.refresh.rejected``,
just the frontend's 45 s reactive-recovery abort entries in the
browser. The route-local timeout converts that silent hang into a
fast 503 + ``auth.refresh.handler_timeout`` warning so the next
recurrence has a request_id to trace.
"""

from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import patch

import pytest


class TestRefreshHandlerTimeout:
    @pytest.mark.asyncio
    async def test_refresh_returns_503_when_inner_impl_hangs(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If ``_refresh_impl`` exceeds the route ceiling the public
        ``refresh()`` must surface a 503 with the Redis-unavailable
        detail string (so the frontend treats it as transient and
        retries on a fresh state) — NOT propagate the
        ``asyncio.TimeoutError`` as a 500. The structured
        ``auth.refresh.handler_timeout`` event must also fire so the
        timeout shows up in production logs."""
        import app.routers.auth as auth_mod
        from fastapi import HTTPException, status

        # Shrink the bound so a regression that drops asyncio.wait_for
        # would fail in ~1 s instead of running the full 60 s mock hang.
        # IMPORTANT: monkeypatch the SAME settings object the auth module
        # already holds (``auth_mod.app_settings``), not a freshly imported
        # ``app.config.settings``. Earlier tests in the suite (e.g.
        # ``test_database_pool_config``) call ``importlib.reload(app_config)``
        # which creates a NEW settings instance, but ``app.routers.auth``
        # keeps its reference to the OLD one — patching the new one
        # would never reach the handler's read path.
        monkeypatch.setattr(auth_mod.app_settings, "refresh_handler_timeout_s", 0.05)

        async def hang_forever(*args, **kwargs):
            await asyncio.sleep(60)

        # Patch _LOGGER on the module so we can assert the structured
        # event regardless of how structlog's processor chain routes
        # output. ``warning`` is the contract surface.
        with patch.object(auth_mod, "_refresh_impl", hang_forever), \
             patch.object(auth_mod, "_LOGGER") as logger_mock:
            start = time.monotonic()
            with pytest.raises(HTTPException) as exc_info:
                await auth_mod.refresh(
                    request=None,  # type: ignore[arg-type]
                    response=None,  # type: ignore[arg-type]
                    db=None,  # type: ignore[arg-type]
                    session_factory=None,  # type: ignore[arg-type]
                )
            elapsed = time.monotonic() - start

        assert exc_info.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert exc_info.value.detail == auth_mod.SESSION_REDIS_UNAVAILABLE_DETAIL
        # Bound must be enforced — generous headroom over the 50 ms cap.
        assert elapsed < 2.0, (
            f"refresh handler took {elapsed:.2f}s; "
            f"asyncio.wait_for not enforced"
        )
        # Structured operator signal: exactly one warning with the
        # event name and the configured timeout value. Operators grep
        # this in production logs to correlate hangs to request_ids.
        logger_mock.warning.assert_called_once()
        call_args, call_kwargs = logger_mock.warning.call_args
        assert call_args[0] == "auth.refresh.handler_timeout"
        assert call_kwargs["extra"]["timeout_s"] == pytest.approx(0.05)

    @pytest.mark.asyncio
    async def test_refresh_passes_through_inner_result_on_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When ``_refresh_impl`` returns normally, ``refresh()`` returns
        the same value (the timeout wrapper is transparent on the
        happy path). A regression that always wrapped in TimeoutError
        would break every successful refresh."""
        import app.routers.auth as auth_mod

        # See sibling test for why this targets ``auth_mod.app_settings``.
        monkeypatch.setattr(auth_mod.app_settings, "refresh_handler_timeout_s", 5.0)

        sentinel = {"access_token": "fake", "expires_in": 900}

        async def quick_success(*args, **kwargs):
            return sentinel

        with patch.object(auth_mod, "_refresh_impl", quick_success):
            result = await auth_mod.refresh(
                request=None,  # type: ignore[arg-type]
                response=None,  # type: ignore[arg-type]
                db=None,  # type: ignore[arg-type]
                session_factory=None,  # type: ignore[arg-type]
            )

        assert result is sentinel

    @pytest.mark.asyncio
    async def test_refresh_passes_through_httpexception_from_inner_impl(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """HTTPException raised by ``_refresh_impl`` (the normal 401 path
        for an expired refresh token, 503 for Redis-down, etc.) must
        propagate unchanged. The timeout wrapper must NOT catch them
        and convert them to its own 503."""
        import app.routers.auth as auth_mod
        from fastapi import HTTPException, status

        # See sibling test for why this targets ``auth_mod.app_settings``.
        monkeypatch.setattr(auth_mod.app_settings, "refresh_handler_timeout_s", 5.0)

        async def reject_401(*args, **kwargs):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid refresh token",
            )

        with patch.object(auth_mod, "_refresh_impl", reject_401):
            with pytest.raises(HTTPException) as exc_info:
                await auth_mod.refresh(
                    request=None,  # type: ignore[arg-type]
                    response=None,  # type: ignore[arg-type]
                    db=None,  # type: ignore[arg-type]
                    session_factory=None,  # type: ignore[arg-type]
                )

        assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
        assert exc_info.value.detail == "Invalid refresh token"

    @pytest.mark.asyncio
    async def test_refresh_passes_through_unexpected_exception(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An unexpected exception (e.g. a real programmer bug, NOT a
        TimeoutError, NOT an HTTPException) must propagate unchanged so
        it surfaces as a 500 with a complete traceback in logs. The
        timeout wrapper must NOT swallow it and convert to a 503 —
        that would mask the actual bug under a misleading
        Redis-unavailable detail."""
        import app.routers.auth as auth_mod

        # See sibling test for why this targets ``auth_mod.app_settings``.
        monkeypatch.setattr(auth_mod.app_settings, "refresh_handler_timeout_s", 5.0)

        class ProgrammerBug(Exception):
            pass

        async def boom(*args, **kwargs):
            raise ProgrammerBug("list index out of range")

        with patch.object(auth_mod, "_refresh_impl", boom):
            with pytest.raises(ProgrammerBug) as exc_info:
                await auth_mod.refresh(
                    request=None,  # type: ignore[arg-type]
                    response=None,  # type: ignore[arg-type]
                    db=None,  # type: ignore[arg-type]
                    session_factory=None,  # type: ignore[arg-type]
                )

        assert "list index out of range" in str(exc_info.value)
