"""End-to-end contract test: the structured fields a call passes in ``extra`` (``error_class``,
``reason``, ...) AND the log context (``request_id``) BOTH appear in the rendered JSON.

This pins the production-observability contract: an ``extra`` the formatter did not render would
silently strip operator-visible fields from every breadcrumb without breaking any caplog-based
test. The ``log_lines`` fixture renders each record through the real filter and formatter.
"""

from __future__ import annotations

from app import logs


def test_state_db_purge_failure_emits_error_class_and_request_id(log_lines, state_db_down) -> None:
    """Contract, driven by a real ``state_db`` log: a structured warning carries both its own
    field (``error_class``) and the ``request_id`` from the context in the rendered JSON. The purge
    swallows the store error and logs it, so a dead engine produces the event."""
    import app.state_db as sd

    logs.bind(request_id="req-test-1234")
    sd._purge_used_tokens()

    events = log_lines()
    assert len(events) == 1
    event = events[0]
    assert event["msg"] == "used_tokens.purge_failed"
    assert event["error_class"] == "OperationalError", event
    assert event["request_id"] == "req-test-1234", event
    assert event["level"] == "WARNING"


def test_state_db_family_purge_failure_emits_error_class_and_request_id(
    log_lines, state_db_down
) -> None:
    """Same contract for the session-family purge warning."""
    import app.state_db as sd

    logs.bind(request_id="req-retired-7")
    sd._purge_families()

    events = log_lines()
    assert len(events) == 1
    event = events[0]
    assert event["msg"] == "auth.session.purge_failed"
    assert event["error_class"] == "OperationalError", event
    assert event["request_id"] == "req-retired-7", event
