"""End-to-end contract test: structured fields a foreign-record
emitter passes (``op``, ``reason``, ``timeout_s``, etc.) AND
contextvars (``request_id``) BOTH appear in the rendered JSON.

This pins the production-observability contract that the 2026-05-20
PR review surfaced: a stdlib ``logger.info(msg, extra={...})`` call
silently drops the ``extra`` fields under the project's
``ProcessorFormatter`` config, while a structlog ``logger.info(msg,
**kwargs)`` carries them through. Without this regression test, a
future refactor that swaps the logger type back can silently strip
operator-visible fields from every breadcrumb without breaking any
caplog-based test.
"""

from __future__ import annotations

import io
import json
import logging
import os

import pytest
import structlog


@pytest.fixture
def captured_stream(monkeypatch: pytest.MonkeyPatch):
    """Wire the structlog ProcessorFormatter from ``app.logging`` to a
    StringIO so we can read what production would have written to
    stdout. Restores the original handler list afterwards so the
    rest of the suite is untouched."""
    # Settings init requires JWT_SECRET_KEY when imported the first
    # time. Ensure it is set before ``app.logging`` is loaded.
    monkeypatch.setenv(
        "JWT_SECRET_KEY",
        "abcdefghijklmnopqrstuvwxyz1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ12",
    )

    from app.logging import setup_logging

    setup_logging()

    buf = io.StringIO()
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    formatter = saved_handlers[0].formatter
    test_handler = logging.StreamHandler(buf)
    test_handler.setFormatter(formatter)
    root.handlers = [test_handler]

    yield buf

    root.handlers = saved_handlers
    structlog.contextvars.clear_contextvars()


def _parse_lines(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().strip().splitlines() if line.strip()]


def test_redis_client_logger_emits_op_and_request_id(captured_stream) -> None:
    """Contract: a structured Redis breadcrumb must include both the
    operator-visible op field and the request_id from contextvars in
    the rendered JSON. If either is missing in production, the
    breadcrumbs are useless for correlation."""
    import app.redis_client as rc

    structlog.contextvars.bind_contextvars(request_id="req-test-1234")
    rc.logger.info("redis.call.start", op="session_validate")
    rc.logger.info(
        "redis.call.ok",
        op="session_validate",
        duration_ms=2.3,
    )

    events = _parse_lines(captured_stream)
    assert len(events) == 2
    for event in events:
        assert event["op"] == "session_validate", (
            f"op field missing from rendered output: {event}"
        )
        assert event["request_id"] == "req-test-1234", (
            f"request_id field missing from rendered output: {event}"
        )
    assert events[0]["event"] == "redis.call.start"
    assert events[1]["event"] == "redis.call.ok"
    assert events[1]["duration_ms"] == 2.3


def test_redis_retired_warning_emits_reason_and_request_id(captured_stream) -> None:
    """Same contract for the existing ``redis.client.retired`` warning
    — the ``reason`` field must reach the rendered JSON. This was
    silently broken before the 2026-05-20 logger switch because the
    stdlib-style ``extra={"reason": ...}`` was dropped by
    ProcessorFormatter."""
    import app.redis_client as rc

    structlog.contextvars.bind_contextvars(request_id="req-retired-7")
    rc.logger.warning("redis.client.retired", reason="OSError: BrokenPipeError: ...")

    events = _parse_lines(captured_stream)
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "redis.client.retired"
    assert event["reason"] == "OSError: BrokenPipeError: ...", (
        f"reason field missing from rendered output: {event}"
    )
    assert event["request_id"] == "req-retired-7"
    assert event["level"] == "warning"
