"""Health probes stay out of the access log (INFRA-83: Route 53 polls
/health/dependencies on prod, ~40k lines a day)."""

from __future__ import annotations

import logging

import pytest

from app.logging import _DropHealthCheck


def _access_record(path: str) -> logging.LogRecord:
    # uvicorn.access formats its message exactly like this.
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0,
        '%s - "%s %s HTTP/%s" %d', ("10.42.0.5:51234", "GET", path, "1.1", 200), None,
    )


@pytest.mark.parametrize("path", ["/health", "/ready", "/health/dependencies"])
def test_probe_paths_are_dropped(path):
    assert _DropHealthCheck().filter(_access_record(path)) is False


@pytest.mark.parametrize("path", ["/api/v1/auth/login", "/health/dependencies/x"])
def test_other_paths_are_logged(path):
    assert _DropHealthCheck().filter(_access_record(path)) is True
