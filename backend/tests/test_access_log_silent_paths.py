"""Health probes stay out of the access log (INFRA-83: Route 53 polls
/health/dependencies on prod, ~40k lines a day), and no access line carries
a query string (INFRA-110)."""

from __future__ import annotations

import io
import json
import logging

import pytest

from app.logging import _AccessLogFilter


def _access_record(path: str) -> logging.LogRecord:
    # uvicorn.access formats its message exactly like this.
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0,
        '%s - "%s %s HTTP/%s" %d', ("10.42.0.5:51234", "GET", path, "1.1", 200), None,
    )


@pytest.mark.parametrize("path", ["/health", "/ready", "/health/dependencies"])
def test_probe_paths_are_dropped(path):
    assert _AccessLogFilter().filter(_access_record(path)) is False


@pytest.mark.parametrize("path", ["/api/v1/auth/login", "/health/dependencies/x"])
def test_other_paths_are_logged(path):
    assert _AccessLogFilter().filter(_access_record(path)) is True


# INFRA-110: the query string carries secrets (OAuth ``code``/``state`` on the
# Google callback, the invite ``token``); none of it may reach stdout.
@pytest.mark.parametrize(
    "target, path",
    [
        ("/api/v1/auth/google/callback?code=SECRET&state=S", "/api/v1/auth/google/callback"),
        ("/api/v1/orgs/invitations/preview?token=SECRET", "/api/v1/orgs/invitations/preview"),
        # httptools' lenient URL parsing lets a tab through into the query.
        ("/api/v1/auth/google/callback?a=1\tcode=SECRET", "/api/v1/auth/google/callback"),
        ("/health?probe=SECRET", None),  # probe with a query is still dropped
    ],
)
def test_access_log_never_carries_the_query_string(target, path):
    from app.logging import setup_logging

    setup_logging()
    access = logging.getLogger("uvicorn.access")
    saved = list(access.handlers)
    buf, raw = io.StringIO(), io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(saved[0].formatter)
    # A plain formatter renders msg % args: catches a fix that only cleans
    # the structured ``path`` field and leaves the secret in the record.
    raw_handler = logging.StreamHandler(raw)
    access.handlers = [handler, raw_handler]
    try:
        # Exactly uvicorn's own call (uvicorn/protocols/http/*_impl.py).
        access.info('%s - "%s %s HTTP/%s" %d', "10.42.0.5:51234", "GET", target, "1.1", 200)
    finally:
        access.handlers = saved

    out = buf.getvalue()
    assert "SECRET" not in out
    assert "SECRET" not in raw.getvalue()
    if path is None:
        assert out == ""
        return
    line = json.loads(out)
    assert (line["event"], line["method"], line["path"], line["status"], line["remote_addr"]) == (
        "request", "GET", path, 200, "10.42.0.5:51234",
    )
