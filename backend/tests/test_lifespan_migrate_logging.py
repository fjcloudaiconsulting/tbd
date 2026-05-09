"""Tests for the dev-mode lifespan migration logging.

Asserts that `_run_migrations()` emits a `migrate.dev.target` (or
`migrate.dev.no_op`) structured event before invoking alembic, with the
current + head revisions and best-effort branch attached. The breadcrumb
exists so the next alembic drift incident has a log line pointing at
exactly which revision the lifespan was targeting.

Unit tests only; alembic subprocess and DB lookups are mocked.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
import structlog
from structlog.testing import LogCapture

from app import main as app_main


@pytest.fixture
def cap_logs():
    """Reroute structlog through LogCapture; restore on teardown."""
    capture = LogCapture()

    structlog.configure(
        processors=[capture],
        wrapper_class=structlog.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )

    # Re-bind the module-level logger so it picks up the new config.
    original_logger = app_main.logger
    app_main.logger = structlog.stdlib.get_logger()

    yield capture

    app_main.logger = original_logger
    structlog.reset_defaults()


@pytest.mark.asyncio
async def test_run_migrations_emits_target_event_with_revisions(cap_logs, monkeypatch):
    """When current != head, we log migrate.dev.target then run alembic."""
    monkeypatch.setattr(app_main, "_resolve_alembic_head", lambda: "head_abc")
    monkeypatch.setattr(app_main, "_resolve_git_branch", lambda: "feat/something")

    async def _fake_current() -> str:
        return "current_xyz"

    monkeypatch.setattr(app_main, "_resolve_alembic_current", _fake_current)

    class _FakeResult:
        returncode = 0
        stderr = ""
        stdout = ""

    monkeypatch.setattr(
        app_main.subprocess, "run", lambda *a, **kw: _FakeResult()
    )

    await app_main._run_migrations()

    events = [e["event"] for e in cap_logs.entries]
    assert "migrate.dev.target" in events

    target_event = next(
        e for e in cap_logs.entries if e["event"] == "migrate.dev.target"
    )
    assert target_event["current_revision"] == "current_xyz"
    assert target_event["head_revision"] == "head_abc"
    assert target_event["branch"] == "feat/something"


@pytest.mark.asyncio
async def test_run_migrations_emits_no_op_when_current_equals_head(cap_logs, monkeypatch):
    """When current == head, we log migrate.dev.no_op and skip subprocess."""
    monkeypatch.setattr(app_main, "_resolve_alembic_head", lambda: "head_abc")
    monkeypatch.setattr(app_main, "_resolve_git_branch", lambda: "main")

    async def _fake_current() -> str:
        return "head_abc"

    monkeypatch.setattr(app_main, "_resolve_alembic_current", _fake_current)

    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("subprocess should not be called on no-op")

    monkeypatch.setattr(app_main.subprocess, "run", _boom)

    await app_main._run_migrations()

    events = [e["event"] for e in cap_logs.entries]
    assert events == ["migrate.dev.no_op"]
    entry = cap_logs.entries[0]
    assert entry["current_revision"] == "head_abc"
    assert entry["head_revision"] == "head_abc"
    assert entry["branch"] == "main"


@pytest.mark.asyncio
async def test_run_migrations_runs_alembic_when_head_unknown(cap_logs, monkeypatch):
    """If head resolution fails, we still log + run alembic (the upgrade
    will then either succeed or surface the real failure)."""
    monkeypatch.setattr(app_main, "_resolve_alembic_head", lambda: "unknown")
    monkeypatch.setattr(app_main, "_resolve_git_branch", lambda: "main")

    async def _fake_current() -> str:
        return "unknown"

    monkeypatch.setattr(app_main, "_resolve_alembic_current", _fake_current)

    calls: list[tuple[Any, ...]] = []

    class _FakeResult:
        returncode = 0
        stderr = ""
        stdout = ""

    def _record(args, **_kw):
        calls.append(tuple(args))
        return _FakeResult()

    monkeypatch.setattr(app_main.subprocess, "run", _record)

    await app_main._run_migrations()

    # Even with both unknown we still emit a target event (no_op only
    # fires when both equal AND head is a real revision).
    events = [e["event"] for e in cap_logs.entries]
    assert "migrate.dev.target" in events
    assert calls == [("alembic", "upgrade", "head")]


@pytest.mark.asyncio
async def test_run_migrations_raises_on_alembic_failure(monkeypatch):
    monkeypatch.setattr(app_main, "_resolve_alembic_head", lambda: "head_abc")
    monkeypatch.setattr(app_main, "_resolve_git_branch", lambda: "main")

    async def _fake_current() -> str:
        return "current_xyz"

    monkeypatch.setattr(app_main, "_resolve_alembic_current", _fake_current)

    class _FakeResult:
        returncode = 1
        stderr = "boom"
        stdout = ""

    monkeypatch.setattr(
        app_main.subprocess, "run", lambda *a, **kw: _FakeResult()
    )

    with pytest.raises(RuntimeError, match="Migration failed"):
        await app_main._run_migrations()


def test_resolve_git_branch_returns_string():
    """Best-effort branch resolution must always return a string, even
    when git is unavailable or times out."""
    branch = app_main._resolve_git_branch()
    assert isinstance(branch, str)
    assert branch  # non-empty


def test_resolve_alembic_head_returns_string():
    """Head resolution must never raise; returns 'unknown' on failure."""
    with patch.object(app_main, "_ALEMBIC_INI_PATH", "/nonexistent/alembic.ini"):
        result = app_main._resolve_alembic_head()
        assert result == "unknown"
