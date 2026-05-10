"""Tests for the dev-mode lifespan migration logging + branch guard.

Covers two layers of `_run_migrations()` behavior:

  1. Branch guard: refuses to run when the host checkout is off main
     unless `PFV_MIGRATE_OK_OFF_MAIN=1` is set. Mirrors `./pfv migrate`.
  2. Logging breadcrumb: emits `migrate.dev.target` /
     `migrate.dev.no_op` with current + head revisions and branch so the
     next alembic drift incident has a structured pointer.

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


@pytest.fixture
def on_main(monkeypatch):
    """Default the environment to a main-branch checkout with no override
    so individual tests only have to override what they exercise. Tests
    that need off-main behavior re-patch `_detect_branch` themselves.
    """
    monkeypatch.setattr(app_main, "_detect_branch", lambda: "main")
    monkeypatch.delenv("PFV_MIGRATE_OK_OFF_MAIN", raising=False)


@pytest.mark.asyncio
async def test_run_migrations_emits_target_event_with_revisions(
    cap_logs, monkeypatch, on_main
):
    """When current != head, we log migrate.dev.target then run alembic."""
    monkeypatch.setattr(app_main, "_resolve_alembic_head", lambda: "head_abc")

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
    assert target_event["branch"] == "main"


@pytest.mark.asyncio
async def test_run_migrations_emits_no_op_when_current_equals_head(
    cap_logs, monkeypatch, on_main
):
    """When current == head, we log migrate.dev.no_op and skip subprocess."""
    monkeypatch.setattr(app_main, "_resolve_alembic_head", lambda: "head_abc")

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
async def test_run_migrations_runs_alembic_when_head_unknown(
    cap_logs, monkeypatch, on_main
):
    """If head resolution fails, we still log + run alembic (the upgrade
    will then either succeed or surface the real failure)."""
    monkeypatch.setattr(app_main, "_resolve_alembic_head", lambda: "unknown")

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
async def test_run_migrations_raises_on_alembic_failure(monkeypatch, on_main):
    monkeypatch.setattr(app_main, "_resolve_alembic_head", lambda: "head_abc")

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


# ----------------------------------------------------------------------
# Branch guard tests: the lifespan must refuse to migrate when the
# host checkout is off main unless PFV_MIGRATE_OK_OFF_MAIN=1 is set.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_migrations_refuses_when_branch_is_not_main(
    cap_logs, monkeypatch
):
    """Off-main branch + no override => RuntimeError, no alembic invoked."""
    monkeypatch.setattr(app_main, "_detect_branch", lambda: "feat/some-thing")
    monkeypatch.delenv("PFV_MIGRATE_OK_OFF_MAIN", raising=False)

    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("subprocess must not run when the guard refuses")

    monkeypatch.setattr(app_main.subprocess, "run", _boom)

    with pytest.raises(RuntimeError, match="Refusing to run dev lifespan migrations"):
        await app_main._run_migrations()

    refused = [e for e in cap_logs.entries if e["event"] == "migrate.dev.refused"]
    assert len(refused) == 1
    assert refused[0]["branch"] == "feat/some-thing"
    assert refused[0]["reason"] == "branch_not_main"
    assert refused[0]["override_env_var"] == "PFV_MIGRATE_OK_OFF_MAIN"


@pytest.mark.asyncio
async def test_run_migrations_proceeds_when_branch_is_main(
    cap_logs, monkeypatch
):
    """main branch + no override => proceeds, alembic invoked."""
    monkeypatch.setattr(app_main, "_detect_branch", lambda: "main")
    monkeypatch.setattr(app_main, "_resolve_alembic_head", lambda: "head_abc")
    monkeypatch.delenv("PFV_MIGRATE_OK_OFF_MAIN", raising=False)

    async def _fake_current() -> str:
        return "current_xyz"

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

    assert calls == [("alembic", "upgrade", "head")]
    events = [e["event"] for e in cap_logs.entries]
    assert "migrate.dev.refused" not in events


@pytest.mark.asyncio
async def test_run_migrations_proceeds_off_main_when_override_set(
    cap_logs, monkeypatch
):
    """Off-main + PFV_MIGRATE_OK_OFF_MAIN=1 => proceeds (escape hatch)."""
    monkeypatch.setattr(app_main, "_detect_branch", lambda: "feat/something")
    monkeypatch.setattr(app_main, "_resolve_alembic_head", lambda: "head_abc")
    monkeypatch.setenv("PFV_MIGRATE_OK_OFF_MAIN", "1")

    async def _fake_current() -> str:
        return "current_xyz"

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

    assert calls == [("alembic", "upgrade", "head")]
    events = [e["event"] for e in cap_logs.entries]
    assert "migrate.dev.refused" not in events
    target = next(e for e in cap_logs.entries if e["event"] == "migrate.dev.target")
    assert target["branch"] == "feat/something"


@pytest.mark.asyncio
async def test_run_migrations_refuses_on_undetectable_branch(
    cap_logs, monkeypatch
):
    """Detached HEAD / unreadable HEAD => fail closed (refuse).

    Tradeoff: this catches detached-HEAD checkouts (rare in dev) at the
    cost of a noisy error. The override env var is the documented
    escape hatch; the alternative (proceed when undetectable) would
    silently re-open the exact drift class the guard exists to close.
    """
    monkeypatch.setattr(app_main, "_detect_branch", lambda: None)
    monkeypatch.delenv("PFV_MIGRATE_OK_OFF_MAIN", raising=False)

    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("subprocess must not run when the guard refuses")

    monkeypatch.setattr(app_main.subprocess, "run", _boom)

    with pytest.raises(RuntimeError, match="detached/unknown"):
        await app_main._run_migrations()

    refused = [e for e in cap_logs.entries if e["event"] == "migrate.dev.refused"]
    assert len(refused) == 1
    assert refused[0]["branch"] == "unknown"
    assert refused[0]["reason"] == "branch_undetectable"


# ----------------------------------------------------------------------
# Direct unit tests for the new helpers.
# ----------------------------------------------------------------------


def test_detect_branch_reads_symbolic_ref(tmp_path, monkeypatch):
    """_detect_branch returns the branch name for a normal HEAD file."""
    head = tmp_path / "HEAD"
    head.write_text("ref: refs/heads/feat/sample\n")
    monkeypatch.setattr(app_main, "_GIT_HEAD_PATH", str(head))

    assert app_main._detect_branch() == "feat/sample"


def test_detect_branch_returns_none_for_detached_head(tmp_path, monkeypatch):
    """A raw SHA in HEAD (detached) returns None: fail closed."""
    head = tmp_path / "HEAD"
    head.write_text("a1b2c3d4e5f6\n")
    monkeypatch.setattr(app_main, "_GIT_HEAD_PATH", str(head))

    assert app_main._detect_branch() is None


def test_detect_branch_returns_none_when_file_missing(tmp_path, monkeypatch):
    """Missing /app/.git/HEAD => None (e.g. .git not bind-mounted)."""
    monkeypatch.setattr(
        app_main, "_GIT_HEAD_PATH", str(tmp_path / "does-not-exist")
    )
    assert app_main._detect_branch() is None


def test_migrate_off_main_override_only_truthy_for_exact_one(monkeypatch):
    """The override is opt-in: only the literal string '1' counts."""
    monkeypatch.delenv("PFV_MIGRATE_OK_OFF_MAIN", raising=False)
    assert app_main._migrate_off_main_override_set() is False

    monkeypatch.setenv("PFV_MIGRATE_OK_OFF_MAIN", "")
    assert app_main._migrate_off_main_override_set() is False

    monkeypatch.setenv("PFV_MIGRATE_OK_OFF_MAIN", "true")
    assert app_main._migrate_off_main_override_set() is False

    monkeypatch.setenv("PFV_MIGRATE_OK_OFF_MAIN", "1")
    assert app_main._migrate_off_main_override_set() is True

    # Whitespace stripped so "  1  " also counts (handy for .env files).
    monkeypatch.setenv("PFV_MIGRATE_OK_OFF_MAIN", " 1 ")
    assert app_main._migrate_off_main_override_set() is True
