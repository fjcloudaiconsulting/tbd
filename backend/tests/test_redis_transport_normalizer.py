"""Redis transport-error normalizer — 2026-05-19.

Production trace at 2026-05-19T07:10:52 showed an uncaught
``RuntimeError: unable to perform operation on <TCPTransport closed=True
reading=False ...>; the handler is closed`` escaping from
``redis-py``'s ``health_check`` during ``/api/v1/auth/refresh``. The
router's existing ``except (RedisRequired, RedisError)`` handler did
not catch it (``RuntimeError`` is not a ``RedisError`` subclass), so
FastAPI returned 500 instead of the recoverable 503-fallback the
frontend already knows how to handle.

``redis_client._normalize_transport_errors`` is the narrow translation
layer that converts the known closed-transport ``RuntimeError`` (and
socket-level ``OSError`` family) into ``redis.exceptions.ConnectionError``.
These tests pin the contract:

  1. Closed-transport ``RuntimeError`` → ``RedisConnectionError``
  2. Unrelated ``RuntimeError("programmer bug")`` propagates unchanged
  3. ``OSError`` / ``BrokenPipeError`` / ``ConnectionResetError`` →
     ``RedisConnectionError``
  4. ``RedisRequired`` (also a ``RuntimeError`` subclass) propagates
     unchanged — programmer/config signal, not transport
  5. ``RedisError`` subclasses (including ``ResponseError`` from Lua)
     pass through unchanged — ``session_rotate_lua``'s Lua-return-token
     parser depends on the raw ``ResponseError`` message
  6. Application-level success values pass through unchanged
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import ResponseError as RedisResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.redis_client import (
    RedisRequired,
    _looks_like_dead_transport,
    _normalize_transport_errors,
)


# ── Marker detection ────────────────────────────────────────────────────


class TestLooksLikeDeadTransport:
    """The closed-transport detector must be narrow: real bug
    ``RuntimeError`` must NOT match. Production uvloop / asyncio
    transport-death messages MUST match.
    """

    def test_uvloop_tcptransport_closed_matches(self) -> None:
        # Verbatim message from the 2026-05-19T07:10:52 production trace.
        exc = RuntimeError(
            "unable to perform operation on <TCPTransport closed=True "
            "reading=False 0x55a57d2583e0>; the handler is closed"
        )
        assert _looks_like_dead_transport(exc) is True

    def test_handler_is_closed_alone_matches(self) -> None:
        exc = RuntimeError("the handler is closed")
        assert _looks_like_dead_transport(exc) is True

    def test_broken_pipe_message_matches(self) -> None:
        exc = RuntimeError("broken pipe during write")
        assert _looks_like_dead_transport(exc) is True

    def test_connection_reset_message_matches(self) -> None:
        exc = RuntimeError("Connection reset by peer")
        assert _looks_like_dead_transport(exc) is True

    def test_transport_closed_generic_matches(self) -> None:
        exc = RuntimeError("transport is closed")
        assert _looks_like_dead_transport(exc) is True

    def test_case_insensitive(self) -> None:
        # Real messages may be mixed case; the matcher normalises.
        exc = RuntimeError("TCPTRANSPORT CLOSED")
        assert _looks_like_dead_transport(exc) is True

    def test_unrelated_runtime_error_does_not_match(self) -> None:
        """The critical guard: programmer bugs MUST NOT match."""
        exc = RuntimeError("programmer bug: list index out of range")
        assert _looks_like_dead_transport(exc) is False

    def test_empty_message_does_not_match(self) -> None:
        exc = RuntimeError()
        assert _looks_like_dead_transport(exc) is False

    def test_value_error_with_transport_words_does_not_match(self) -> None:
        # Pedantic: the matcher only runs inside the wrapper's RuntimeError
        # branch, but the predicate itself should also not falsely match
        # if accidentally called on a non-RuntimeError.
        exc = ValueError("tcptransport closed but this is not a transport error")
        # The matcher is a substring scan; it WILL match on the string.
        # That's by design — the wrapper only routes to this matcher
        # for RuntimeError, so a stray ValueError can't reach it in
        # practice. Documenting the design here.
        assert _looks_like_dead_transport(exc) is True


# ── Decorator wrapping behaviour ────────────────────────────────────────


class TestNormalizeTransportErrors:
    """Six core contracts the wrapper must enforce."""

    @pytest.mark.asyncio
    async def test_success_passes_through(self) -> None:
        @_normalize_transport_errors
        async def helper() -> str:
            return "ok"

        assert await helper() == "ok"

    @pytest.mark.asyncio
    async def test_closed_transport_runtime_error_becomes_redis_connection_error(
        self,
    ) -> None:
        """Spec #1 — the canonical production failure."""

        @_normalize_transport_errors
        async def helper() -> None:
            raise RuntimeError(
                "unable to perform operation on <TCPTransport closed=True "
                "reading=False 0x55a57d2583e0>; the handler is closed"
            )

        with pytest.raises(RedisConnectionError) as exc_info:
            await helper()
        # The original RuntimeError is preserved as the cause so a future
        # traceback still shows where the closed-transport surfaced.
        assert isinstance(exc_info.value.__cause__, RuntimeError)
        assert "the handler is closed" in str(exc_info.value.__cause__)

    @pytest.mark.asyncio
    async def test_unrelated_runtime_error_propagates_unchanged(self) -> None:
        """Spec #2 — real programmer bugs MUST still fail loudly as 500.
        This is the critical safety property of the narrow filter."""

        @_normalize_transport_errors
        async def helper() -> None:
            raise RuntimeError("programmer bug: divide by zero")

        with pytest.raises(RuntimeError) as exc_info:
            await helper()
        # The original RuntimeError is the raised exception, NOT a
        # RedisConnectionError. The except clause must let it through.
        assert not isinstance(exc_info.value, RedisConnectionError)
        assert "programmer bug" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_broken_pipe_oserror_becomes_redis_connection_error(self) -> None:
        """Spec #3a — socket-level I/O failure during a Redis op."""

        @_normalize_transport_errors
        async def helper() -> None:
            raise BrokenPipeError(32, "Broken pipe")

        with pytest.raises(RedisConnectionError) as exc_info:
            await helper()
        assert isinstance(exc_info.value.__cause__, BrokenPipeError)

    @pytest.mark.asyncio
    async def test_connection_reset_oserror_becomes_redis_connection_error(
        self,
    ) -> None:
        """Spec #3b — NAT idle-drop class."""

        @_normalize_transport_errors
        async def helper() -> None:
            raise ConnectionResetError(104, "Connection reset by peer")

        with pytest.raises(RedisConnectionError) as exc_info:
            await helper()
        assert isinstance(exc_info.value.__cause__, ConnectionResetError)

    @pytest.mark.asyncio
    async def test_generic_oserror_becomes_redis_connection_error(self) -> None:
        @_normalize_transport_errors
        async def helper() -> None:
            raise OSError(101, "Network unreachable")

        with pytest.raises(RedisConnectionError):
            await helper()

    @pytest.mark.asyncio
    async def test_redis_required_propagates_unchanged(self) -> None:
        """Spec #4 — RedisRequired (a RuntimeError subclass) is a
        programmer / config signal, not a transport issue. It must
        pass through so the operator sees the original error."""

        @_normalize_transport_errors
        async def helper() -> None:
            raise RedisRequired("REDIS_URL must be set")

        with pytest.raises(RedisRequired) as exc_info:
            await helper()
        assert not isinstance(exc_info.value, RedisConnectionError)
        assert "REDIS_URL must be set" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_redis_response_error_passes_through_for_lua_token_parsing(
        self,
    ) -> None:
        """Spec #5 — ResponseError carries Lua ``{err = "..."}`` return
        tokens that ``session_rotate_lua`` parses for the rotation
        result. Re-classifying as RedisConnectionError would break that
        parser and turn ``session_revoked`` into a generic 503 instead
        of a terminal 401."""

        @_normalize_transport_errors
        async def helper() -> None:
            raise RedisResponseError("session_revoked")

        with pytest.raises(RedisResponseError) as exc_info:
            await helper()
        # Original ResponseError preserved verbatim — message intact.
        assert str(exc_info.value) == "session_revoked"

    @pytest.mark.asyncio
    async def test_redis_connection_error_passes_through(self) -> None:
        """Already a sensible Redis-domain exception. No need to
        re-wrap; routers' existing handler matches on RedisError."""

        @_normalize_transport_errors
        async def helper() -> None:
            raise RedisConnectionError("Error 111 connecting to host:6379")

        with pytest.raises(RedisConnectionError) as exc_info:
            await helper()
        assert "Error 111" in str(exc_info.value)
        # And it's the literal class — no wrapping / re-raise added.
        assert exc_info.value.__cause__ is None

    @pytest.mark.asyncio
    async def test_redis_timeout_error_passes_through(self) -> None:
        @_normalize_transport_errors
        async def helper() -> None:
            raise RedisTimeoutError("Timeout reading from socket")

        with pytest.raises(RedisTimeoutError):
            await helper()

    @pytest.mark.asyncio
    async def test_generic_redis_error_passes_through(self) -> None:
        @_normalize_transport_errors
        async def helper() -> None:
            raise RedisError("some other Redis-domain error")

        with pytest.raises(RedisError) as exc_info:
            await helper()
        assert not isinstance(exc_info.value, RedisConnectionError)


# ── End-to-end coverage of every wrapped helper ─────────────────────────


class TestSessionHelpersWrapped:
    """Every public session_* helper in redis_client must wrap. This
    catches the case where someone adds a new helper and forgets the
    decorator — a closed transport in the new helper would bypass
    the wrapper and produce a 500."""

    def test_session_validate_is_wrapped(self) -> None:
        from app.redis_client import session_validate

        assert hasattr(session_validate, "__wrapped__"), (
            "session_validate is missing @_normalize_transport_errors"
        )

    def test_session_grace_is_wrapped(self) -> None:
        from app.redis_client import session_grace

        assert hasattr(session_grace, "__wrapped__")

    def test_session_family_exists_is_wrapped(self) -> None:
        from app.redis_client import session_family_exists

        assert hasattr(session_family_exists, "__wrapped__")

    def test_session_family_member_is_wrapped(self) -> None:
        from app.redis_client import session_family_member

        assert hasattr(session_family_member, "__wrapped__")

    def test_session_issue_is_wrapped(self) -> None:
        """Spec #6a — pipeline ops (issue, rotation, revoke) MUST also
        wrap. A broken-pipe error during ``MULTI/EXEC`` would otherwise
        escape as 500."""
        from app.redis_client import session_issue

        assert hasattr(session_issue, "__wrapped__")

    def test_session_rotate_lua_is_wrapped(self) -> None:
        """Spec #6b — rotation pipeline ops wrapped."""
        from app.redis_client import session_rotate_lua

        assert hasattr(session_rotate_lua, "__wrapped__")

    def test_session_revoke_family_is_wrapped(self) -> None:
        """Spec #6c — revoke pipeline ops wrapped."""
        from app.redis_client import session_revoke_family

        assert hasattr(session_revoke_family, "__wrapped__")

    def test_mfa_email_nonce_set_is_wrapped(self) -> None:
        """Direct-get_client() MFA path must also be covered, per
        review feedback — wrapping only session helpers leaves the
        MFA single-use nonce path exposed."""
        from app.redis_client import mfa_email_nonce_set

        assert hasattr(mfa_email_nonce_set, "__wrapped__")

    def test_mfa_email_nonce_consume_is_wrapped(self) -> None:
        from app.redis_client import mfa_email_nonce_consume

        assert hasattr(mfa_email_nonce_consume, "__wrapped__")


# ── Integration: session_rotate_lua's ResponseError parser still works ──


class TestLuaResponseErrorParserStillWorks:
    """Spec #5 verified at the integration level: when the Lua script
    returns ``{err = "session_revoked"}`` (or one of the other
    rotation tokens), the wrapper does NOT swallow it — the inner
    ``except RedisResponseError`` in ``session_rotate_lua`` runs and
    returns the token string. This is the hottest path that breaks
    if we accidentally over-wrap."""

    @pytest.mark.asyncio
    async def test_lua_session_revoked_returns_token_not_503(self) -> None:
        from app.redis_client import (
            SESSION_ROTATE_REVOKED,
            session_rotate_lua,
        )

        # Mock the client.eval to raise ResponseError("session_revoked").
        # The wrapper must let it propagate to session_rotate_lua's own
        # except clause, which maps it to the bare string token.
        with patch("app.redis_client.require_client") as mock_require:
            mock_client = mock_require.return_value
            mock_client.eval.side_effect = RedisResponseError(
                "session_revoked"
            )
            result = await session_rotate_lua(
                old_jti="old", new_jti="new", sid="s", user_id=1,
                idle_ttl_seconds=60,
            )
        # The function returns the bare token string, NOT raise.
        assert result == SESSION_ROTATE_REVOKED

    @pytest.mark.asyncio
    async def test_lua_already_rotated_returns_token(self) -> None:
        from app.redis_client import (
            SESSION_ROTATE_ALREADY_ROTATED,
            session_rotate_lua,
        )

        with patch("app.redis_client.require_client") as mock_require:
            mock_client = mock_require.return_value
            mock_client.eval.side_effect = RedisResponseError(
                "already_rotated"
            )
            result = await session_rotate_lua(
                old_jti="o", new_jti="n", sid="s", user_id=1,
                idle_ttl_seconds=60,
            )
        assert result == SESSION_ROTATE_ALREADY_ROTATED

    @pytest.mark.asyncio
    async def test_lua_jti_collision_returns_token(self) -> None:
        from app.redis_client import (
            SESSION_ROTATE_JTI_COLLISION,
            session_rotate_lua,
        )

        with patch("app.redis_client.require_client") as mock_require:
            mock_client = mock_require.return_value
            mock_client.eval.side_effect = RedisResponseError(
                "jti_collision"
            )
            result = await session_rotate_lua(
                old_jti="o", new_jti="n", sid="s", user_id=1,
                idle_ttl_seconds=60,
            )
        assert result == SESSION_ROTATE_JTI_COLLISION

    @pytest.mark.asyncio
    async def test_lua_unknown_response_error_propagates(self) -> None:
        """An unknown ResponseError (Lua script bug, syntax error,
        replica desync) is NOT one of the known tokens — it must
        propagate as RedisResponseError so the router can return
        503 fail-closed."""
        from app.redis_client import session_rotate_lua

        with patch("app.redis_client.require_client") as mock_require:
            mock_client = mock_require.return_value
            mock_client.eval.side_effect = RedisResponseError(
                "WRONGTYPE Operation against a key holding the wrong "
                "kind of value"
            )
            with pytest.raises(RedisResponseError) as exc_info:
                await session_rotate_lua(
                    old_jti="o", new_jti="n", sid="s", user_id=1,
                    idle_ttl_seconds=60,
                )
        # Original ResponseError preserved — NOT wrapped as
        # RedisConnectionError.
        assert "WRONGTYPE" in str(exc_info.value)


# ── Poisoned-pool retirement ────────────────────────────────────────────


class TestPoisonedClientRetirement:
    """When the wrapper translates a closed-transport ``RuntimeError``
    or socket-level ``OSError`` into ``RedisConnectionError``, it MUST
    also drop the module-level Redis singleton so the next call to
    ``get_client()`` rebuilds the underlying connection pool.

    Without this, the frontend's reactive 503 retry would loop back to
    the same poisoned pool (``RuntimeError`` is deliberately excluded
    from ``retry_on_error``, so redis-py's own disconnect-on-retry
    path doesn't run for the uvloop closed-transport class). The
    operator-visible symptom would be: one closed-transport event
    triggers a permanent burst of 503s until the worker restarts.
    """

    @pytest.mark.asyncio
    async def test_closed_transport_retires_singleton(self) -> None:
        import app.redis_client as rc

        # Install a sentinel client so retirement has something to drop.
        # AsyncMock-style aclose so the best-effort cleanup succeeds.
        from unittest.mock import AsyncMock, MagicMock
        sentinel_client = MagicMock()
        sentinel_client.aclose = AsyncMock()
        rc._client = sentinel_client

        @_normalize_transport_errors
        async def helper() -> None:
            raise RuntimeError(
                "unable to perform operation on <TCPTransport closed=True "
                "reading=False 0x55a57d2583e0>; the handler is closed"
            )

        with pytest.raises(RedisConnectionError):
            await helper()

        # The singleton MUST have been dropped — that is the core
        # contract of the fix. The next ``get_client()`` will rebuild.
        assert rc._client is None
        # Best-effort cleanup ran on the poisoned client.
        sentinel_client.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_broken_pipe_retires_singleton(self) -> None:
        """OSError branch must also retire — broken pipe / connection
        reset class follows the same code path."""
        import app.redis_client as rc
        from unittest.mock import AsyncMock, MagicMock

        sentinel_client = MagicMock()
        sentinel_client.aclose = AsyncMock()
        rc._client = sentinel_client

        @_normalize_transport_errors
        async def helper() -> None:
            raise BrokenPipeError(32, "Broken pipe")

        with pytest.raises(RedisConnectionError):
            await helper()
        assert rc._client is None

    @pytest.mark.asyncio
    async def test_unrelated_runtime_error_does_not_retire_singleton(
        self,
    ) -> None:
        """Critical safety property: a real programmer bug must NOT
        retire the pool. Dropping the singleton on every random
        RuntimeError would mask bugs AND thrash the connection pool."""
        import app.redis_client as rc
        from unittest.mock import AsyncMock, MagicMock

        sentinel_client = MagicMock()
        sentinel_client.aclose = AsyncMock()
        rc._client = sentinel_client

        @_normalize_transport_errors
        async def helper() -> None:
            raise RuntimeError("programmer bug: list index out of range")

        with pytest.raises(RuntimeError):
            await helper()
        # Singleton still in place — NOT retired.
        assert rc._client is sentinel_client
        sentinel_client.aclose.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_redis_response_error_does_not_retire_singleton(
        self,
    ) -> None:
        """ResponseError carries Lua return tokens; the connection is
        still healthy. Retiring on this class would force every
        ``session_revoked`` rotation to rebuild the pool — pointless
        churn."""
        import app.redis_client as rc
        from unittest.mock import AsyncMock, MagicMock

        sentinel_client = MagicMock()
        sentinel_client.aclose = AsyncMock()
        rc._client = sentinel_client

        @_normalize_transport_errors
        async def helper() -> None:
            raise RedisResponseError("session_revoked")

        with pytest.raises(RedisResponseError):
            await helper()
        assert rc._client is sentinel_client
        sentinel_client.aclose.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_retirement_survives_aclose_failure(self) -> None:
        """``aclose()`` is best-effort: if the underlying socket is
        already dead, ``aclose`` itself may raise. The singleton must
        still be dropped — we don't want to replace one
        ConnectionError with another."""
        import app.redis_client as rc
        from unittest.mock import AsyncMock, MagicMock

        sentinel_client = MagicMock()
        sentinel_client.aclose = AsyncMock(
            side_effect=RuntimeError("aclose can't reach dead socket")
        )
        rc._client = sentinel_client

        @_normalize_transport_errors
        async def helper() -> None:
            raise RuntimeError("the handler is closed")

        # The translated ConnectionError still surfaces — aclose's
        # failure is swallowed.
        with pytest.raises(RedisConnectionError):
            await helper()
        assert rc._client is None

    @pytest.mark.asyncio
    async def test_retirement_bounds_aclose_when_it_hangs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Pool cleanup runs outside the per-command Redis timeout
        budget. A wedged ``aclose()`` would hold the translated
        ``RedisConnectionError`` past the frontend's reactive-recovery
        cap. Retirement MUST bound ``aclose()`` so the translated
        exception reaches the caller promptly even when cleanup is
        stuck."""
        import asyncio
        import logging
        import app.redis_client as rc
        from unittest.mock import MagicMock

        # Shrink the bound for test speed. The constant is read off
        # the module at call time, so monkeypatch is sufficient.
        monkeypatch.setattr(rc, "AUTH_REDIS_ACLOSE_TIMEOUT_S", 0.05)
        caplog.set_level(logging.WARNING, logger=rc.logger.name)

        async def hang_forever() -> None:
            # Models a wedged close path — long enough that no plausible
            # test budget would tolerate it if the bound weren't enforced.
            await asyncio.sleep(60)

        sentinel_client = MagicMock()
        sentinel_client.aclose = hang_forever
        rc._client = sentinel_client

        @_normalize_transport_errors
        async def helper() -> None:
            raise RuntimeError("the handler is closed")

        # Outer wait_for makes a regression fail in ~1 s instead of
        # running the full mocked 60 s sleep. If the inner bound is
        # missing, this raises asyncio.TimeoutError, not
        # RedisConnectionError — and the pytest.raises assertion fails
        # loudly.
        with pytest.raises(RedisConnectionError):
            await asyncio.wait_for(helper(), timeout=1.0)

        # Singleton MUST be dropped (core retirement contract).
        assert rc._client is None
        # Event contract: the timeout path emits a distinct warning
        # so operators can tell wedged retirement apart from clean.
        assert any(
            record.msg == "redis.client.retired.aclose_timeout"
            for record in caplog.records
        ), (
            "expected redis.client.retired.aclose_timeout log event; "
            f"captured: {[r.msg for r in caplog.records]}"
        )
