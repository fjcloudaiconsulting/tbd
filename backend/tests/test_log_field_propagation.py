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


def test_state_db_purge_failure_emits_error_class_and_request_id(
    captured_stream, state_db_down
) -> None:
    """Contract, driven by a real ``state_db`` log: a structured warning
    carries both its own field (``error_class``) and the ``request_id`` from
    contextvars in the rendered JSON. The purge swallows the store error and
    logs it, so a dead engine produces the event."""
    import app.state_db as sd

    structlog.contextvars.bind_contextvars(request_id="req-test-1234")
    sd._purge_used_tokens()

    events = _parse_lines(captured_stream)
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "used_tokens.purge_failed"
    assert event["error_class"] == "OperationalError", event
    assert event["request_id"] == "req-test-1234", event
    assert event["level"] == "warning"


def test_state_db_family_purge_failure_emits_error_class_and_request_id(
    captured_stream, state_db_down
) -> None:
    """Same contract for the session-family purge warning."""
    import app.state_db as sd

    structlog.contextvars.bind_contextvars(request_id="req-retired-7")
    sd._purge_families()

    events = _parse_lines(captured_stream)
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "auth.session.purge_failed"
    assert event["error_class"] == "OperationalError", event
    assert event["request_id"] == "req-retired-7", event
