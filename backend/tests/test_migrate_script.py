"""Tests for the structured-logging migrate wrapper.

Unit tests only; no real DB / no real alembic invocation. We mock
ScriptDirectory, the current-revision lookup, and subprocess.Popen, then
inspect the structlog events the script emits via testing.LogCapture.

Coverage:
  * no-op (current == head)
  * multi-step success (event order, fields, applied_count)
  * alembic non-zero exit propagation
  * multi-head detection (no upgrade attempted, exit non-zero)
  * DB URL redaction (password / host / user never appear in events)
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import structlog
from structlog.testing import LogCapture

from scripts import migrate


@pytest.fixture
def cap_logs():
    """Reroute structlog through LogCapture; restore on teardown.

    Calling migrate.setup_logging() inside main() reconfigures structlog
    with the JSON pipeline. We re-configure AFTER setup_logging runs (via
    a side-effect on the patch) so the capturing processor wins.
    """
    capture = LogCapture()

    original_configure = structlog.configure

    def _configure_with_capture(*args: Any, **kwargs: Any) -> None:
        # Replace processors with the capture so events land in `capture`.
        structlog.configure(
            processors=[capture],
            wrapper_class=structlog.BoundLogger,
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(),
            cache_logger_on_first_use=False,
        )

    # Patch app.logging.setup_logging where the migrate module imports it.
    with patch.object(migrate, "setup_logging", _configure_with_capture):
        yield capture

    # Restore default config so other tests aren't affected.
    structlog.reset_defaults()


def _fake_revision(revision: str, doc: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(revision=revision, doc=doc)


class _FakeScriptDirectory:
    def __init__(self, heads: list[str], revisions: list[SimpleNamespace]):
        self._heads = heads
        # revisions are in apply order (oldest -> newest); iterate_revisions
        # returns newest-first, excluding the lower bound.
        self._revisions = revisions

    def get_heads(self) -> list[str]:
        return list(self._heads)

    def iterate_revisions(self, upper: str, lower: str | None):
        revs_newest_first = list(reversed(self._revisions))
        if lower is None:
            return iter(revs_newest_first)
        # Exclude the lower bound (alembic semantics).
        out: list[SimpleNamespace] = []
        for r in revs_newest_first:
            if r.revision == lower:
                break
            out.append(r)
        return iter(out)


def _patch_alembic(monkeypatch, *, heads: list[str], revisions: list[SimpleNamespace]):
    fake = _FakeScriptDirectory(heads=heads, revisions=revisions)
    monkeypatch.setattr(
        migrate.ScriptDirectory, "from_config", classmethod(lambda cls, cfg: fake)
    )


def _set_database_url(monkeypatch, url: str | None) -> None:
    if url is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", url)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_op_when_current_equals_head(cap_logs, monkeypatch):
    revs = [_fake_revision("001"), _fake_revision("002")]
    _patch_alembic(monkeypatch, heads=["002"], revisions=revs)
    _set_database_url(monkeypatch, "mysql+aiomysql://u:p@h/dbname")

    monkeypatch.setattr(migrate, "_get_current_revision_sync", lambda url: "002")

    # subprocess must NOT be invoked on a no-op.
    def _boom(*_a, **_kw):
        raise AssertionError("subprocess should not be called on no-op")

    monkeypatch.setattr(migrate.subprocess, "Popen", _boom)

    rc = migrate.main()
    assert rc == 0

    events = [e["event"] for e in cap_logs.entries]
    assert events == ["migrate.no_op"]
    entry = cap_logs.entries[0]
    assert entry["revision"] == "002"
    assert entry.get("dialect") == "mysql"
    assert entry.get("database") == "dbname"


def test_multi_step_success_emits_full_event_sequence(cap_logs, monkeypatch):
    revs = [
        _fake_revision("a", doc="first"),
        _fake_revision("b", doc="second"),
        _fake_revision("c", doc="third"),
    ]
    _patch_alembic(monkeypatch, heads=["c"], revisions=revs)
    _set_database_url(monkeypatch, "mysql+aiomysql://u:p@h/dbname")
    monkeypatch.setattr(migrate, "_get_current_revision_sync", lambda url: "a")

    calls: list[str] = []

    def _fake_run(rev: str) -> int:
        calls.append(rev)
        return 0

    monkeypatch.setattr(migrate, "_run_alembic_upgrade", _fake_run)

    rc = migrate.main()
    assert rc == 0

    # Each pending rev (b, c) was upgraded in order.
    assert calls == ["b", "c"]

    events = [e["event"] for e in cap_logs.entries]
    assert events == [
        "migrate.start",
        "migrate.step.start",
        "migrate.step.end",
        "migrate.step.start",
        "migrate.step.end",
        "migrate.complete",
    ]

    start = cap_logs.entries[0]
    assert start["from_revision"] == "a"
    assert start["to_revision"] == "c"
    assert start["step_count"] == 2

    step1_start = cap_logs.entries[1]
    assert step1_start["revision"] == "b"
    assert step1_start["step_index"] == 1
    assert step1_start["step_count"] == 2
    assert step1_start["description"] == "second"

    step1_end = cap_logs.entries[2]
    assert step1_end["revision"] == "b"
    assert step1_end["step_index"] == 1
    assert step1_end["returncode"] == 0
    assert isinstance(step1_end["duration_ms"], int)

    step2_start = cap_logs.entries[3]
    assert step2_start["revision"] == "c"
    assert step2_start["step_index"] == 2
    assert step2_start["description"] == "third"

    complete = cap_logs.entries[5]
    assert complete["from_revision"] == "a"
    assert complete["to_revision"] == "c"
    assert complete["applied_count"] == 2
    assert isinstance(complete["duration_ms"], int)


def test_alembic_nonzero_exit_propagates(cap_logs, monkeypatch):
    revs = [_fake_revision("a"), _fake_revision("b"), _fake_revision("c")]
    _patch_alembic(monkeypatch, heads=["c"], revisions=revs)
    _set_database_url(monkeypatch, "mysql+aiomysql://u:p@h/dbname")
    monkeypatch.setattr(migrate, "_get_current_revision_sync", lambda url: "a")

    def _fake_run(rev: str) -> int:
        # Second pending revision fails.
        return 0 if rev == "b" else 7

    monkeypatch.setattr(migrate, "_run_alembic_upgrade", _fake_run)

    rc = migrate.main()
    assert rc == 7

    events = [e["event"] for e in cap_logs.entries]
    assert events == [
        "migrate.start",
        "migrate.step.start",  # b
        "migrate.step.end",  # b
        "migrate.step.start",  # c
        "migrate.failed",  # c
    ]
    failed = cap_logs.entries[-1]
    assert failed["revision"] == "c"
    assert failed["step_index"] == 2
    assert failed["step_count"] == 2
    assert failed["returncode"] == 7
    assert failed["reason"] == "alembic_nonzero_exit"
    assert isinstance(failed["duration_ms"], int)


def test_multi_head_detection_refuses(cap_logs, monkeypatch):
    revs = [_fake_revision("a"), _fake_revision("b")]
    _patch_alembic(monkeypatch, heads=["a", "b"], revisions=revs)
    _set_database_url(monkeypatch, "mysql+aiomysql://u:p@h/dbname")

    # Should not be called.
    def _boom_url(_url):
        raise AssertionError(
            "_get_current_revision_sync should not run on multi-head"
        )

    def _boom_run(_rev):
        raise AssertionError(
            "_run_alembic_upgrade should not run on multi-head"
        )

    monkeypatch.setattr(migrate, "_get_current_revision_sync", _boom_url)
    monkeypatch.setattr(migrate, "_run_alembic_upgrade", _boom_run)

    rc = migrate.main()
    assert rc == 1

    assert len(cap_logs.entries) == 1
    failed = cap_logs.entries[0]
    assert failed["event"] == "migrate.failed"
    assert failed["reason"] == "multiple_heads"
    assert failed["heads"] == ["a", "b"]
    assert failed["returncode"] == 1


def test_db_url_redaction_no_password_or_host_or_user(cap_logs, monkeypatch):
    """Sweep every emitted event in every code path to confirm secrets
    never reach the log stream.

    Tries each major exit path (no-op, success, failure, multi-head) so
    one regression in any of them shows up here.
    """
    secret_url = "mysql+aiomysql://supersecretuser:supersecretpassword@db.internal:3306/dbname"
    forbidden = [
        "supersecretuser",
        "supersecretpassword",
        "db.internal",
        ":3306",
    ]

    def _assert_clean() -> None:
        for entry in cap_logs.entries:
            blob = repr(entry)
            for token in forbidden:
                assert token not in blob, (
                    f"forbidden token {token!r} leaked into event: {entry!r}"
                )

    # 1. no-op path
    revs = [_fake_revision("001")]
    _patch_alembic(monkeypatch, heads=["001"], revisions=revs)
    _set_database_url(monkeypatch, secret_url)
    monkeypatch.setattr(migrate, "_get_current_revision_sync", lambda url: "001")
    monkeypatch.setattr(
        migrate.subprocess,
        "Popen",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("no subprocess on no-op")
        ),
    )
    assert migrate.main() == 0
    _assert_clean()
    cap_logs.entries.clear()

    # 2. success path
    revs = [_fake_revision("a"), _fake_revision("b")]
    _patch_alembic(monkeypatch, heads=["b"], revisions=revs)
    monkeypatch.setattr(migrate, "_get_current_revision_sync", lambda url: "a")
    monkeypatch.setattr(migrate, "_run_alembic_upgrade", lambda rev: 0)
    assert migrate.main() == 0
    _assert_clean()
    cap_logs.entries.clear()

    # 3. failure path
    monkeypatch.setattr(migrate, "_run_alembic_upgrade", lambda rev: 5)
    assert migrate.main() == 5
    _assert_clean()
    cap_logs.entries.clear()

    # 4. multi-head path
    _patch_alembic(monkeypatch, heads=["a", "b"], revisions=revs)
    assert migrate.main() == 1
    _assert_clean()


def test_safe_url_fields_extracts_only_dialect_and_database():
    """Direct unit check on the redaction helper."""
    fields = migrate._safe_url_fields(
        "mysql+aiomysql://u:p@h:3306/mydb"
    )
    assert fields == {"dialect": "mysql", "database": "mydb"}


def test_safe_url_fields_returns_empty_on_garbage():
    assert migrate._safe_url_fields(None) == {}
    assert migrate._safe_url_fields("") == {}
    # An unparseable URL should yield {} rather than raising.
    bad = migrate._safe_url_fields("\x00not a url\x00")
    assert isinstance(bad, dict)
    # Critically: nothing leaked.
    for token in ("not", "a", "url"):
        assert token not in repr(bad)
