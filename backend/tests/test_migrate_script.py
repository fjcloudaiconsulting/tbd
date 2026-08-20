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

import io
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


# ---------------------------------------------------------------------------
# Connection resilience (TBD-424)
#
# On 2026-08-20 the PRE_DEPLOY migrate job failed at connect time
# (`step_count: 0`, `revision: null`), which failed the deployment and triggered
# an automated rollback of production. The wrapper made exactly ONE connection
# attempt, and the failure event carried only `error_type: OperationalError` --
# so the record could not distinguish a network blip from bad credentials, and
# the incident has no proven root cause.
#
# These fences cover both halves of the fix: retry the transient case, and
# record enough to name the deterministic one WITHOUT reintroducing the
# credential leak the redaction exists to prevent.
# ---------------------------------------------------------------------------


class _FakeDriverError(Exception):
    """Shaped like a DBAPI error wrapped by SQLAlchemy: `.orig.args = (code, msg)`."""

    def __init__(self, code: int, message: str):
        super().__init__(f"({code}, {message!r})")
        self.orig = SimpleNamespace(args=(code, message))


def test_driver_errno_extracts_the_code(monkeypatch):
    assert migrate._driver_errno(_FakeDriverError(2003, "Can't connect")) == 2003
    assert migrate._driver_errno(_FakeDriverError(1045, "Access denied")) == 1045


def test_driver_errno_is_none_when_there_is_no_driver_code():
    assert migrate._driver_errno(ValueError("no orig at all")) is None
    assert migrate._driver_errno(RuntimeError()) is None
    # A driver-ish exception whose first arg is not an int must not be coerced.
    weird = SimpleNamespace(orig=SimpleNamespace(args=("not-a-code", "msg")))
    assert migrate._driver_errno(weird) is None  # type: ignore[arg-type]


def test_driver_errno_never_returns_the_message_string():
    """⚠ The redaction guarantee. `args[1]` is the driver's message and embeds
    username/host ("Access denied for user 'foo'@'10.1.2.3'"). Only `args[0]`
    may ever leave this helper."""
    secret = "Access denied for user 'pfv_app'@'10.108.0.3'"
    got = migrate._driver_errno(_FakeDriverError(1045, secret))
    assert got == 1045
    assert not isinstance(got, str)
    assert secret not in str(got)


@pytest.mark.parametrize(
    "errno,expected",
    [
        (2003, True),   # can't connect
        (2006, True),   # server has gone away
        (2013, True),   # lost connection
        (4031, True),   # idle timeout
        (1045, False),  # access denied - deterministic, do NOT retry
        (1049, False),  # unknown database - deterministic
        (1040, False),  # too many connections - retrying makes it worse
        (1146, False),  # table doesn't exist
    ],
)
def test_is_retryable_only_for_transient_driver_errors(errno, expected):
    assert migrate._is_retryable(_FakeDriverError(errno, "x")) is expected


def test_is_retryable_for_socket_errors_that_never_reached_mysql():
    assert migrate._is_retryable(OSError("connection refused")) is True
    assert migrate._is_retryable(ValueError("programming error")) is False


def test_transient_connect_is_retried_and_then_succeeds(cap_logs, monkeypatch):
    """The whole point: a blip must not fail the deploy."""
    revs = [_fake_revision("001")]
    _patch_alembic(monkeypatch, heads=["001"], revisions=revs)
    _set_database_url(monkeypatch, "mysql+aiomysql://u:p@h/dbname")
    monkeypatch.setattr(migrate, "CONNECT_BACKOFF_BASE_SECONDS", 0)

    calls = {"n": 0}

    def _flaky(_url):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _FakeDriverError(2003, "Can't connect to MySQL server")
        return "001"  # equal to head -> no_op, so no subprocess needed

    monkeypatch.setattr(migrate, "_get_current_revision_sync", _flaky)

    rc = migrate.main()

    assert rc == 0, "a transient connect failure must not fail the migrate job"
    assert calls["n"] == 3
    retries = [e for e in cap_logs.entries if e["event"] == "migrate.connect.retry"]
    assert len(retries) == 2
    assert retries[0]["driver_errno"] == 2003
    assert retries[0]["retryable"] is True
    assert [e["event"] for e in cap_logs.entries][-1] == "migrate.no_op"


def test_non_retryable_connect_fails_immediately_without_burning_the_window(
    cap_logs, monkeypatch
):
    """⚠ Bad credentials are deterministic. Retrying them delays the failure by
    the full backoff and makes the logs read like a flaky network."""
    _patch_alembic(monkeypatch, heads=["001"], revisions=[_fake_revision("001")])
    _set_database_url(monkeypatch, "mysql+aiomysql://u:p@h/dbname")
    monkeypatch.setattr(migrate, "CONNECT_BACKOFF_BASE_SECONDS", 0)

    calls = {"n": 0}

    def _denied(_url):
        calls["n"] += 1
        raise _FakeDriverError(1045, "Access denied for user 'x'@'10.1.2.3'")

    monkeypatch.setattr(migrate, "_get_current_revision_sync", _denied)

    rc = migrate.main()

    assert rc == 1
    assert calls["n"] == 1, "a deterministic failure must not be retried"
    failed = [e for e in cap_logs.entries if e["event"] == "migrate.failed"]
    assert failed and failed[-1]["driver_errno"] == 1045


def test_persistent_transient_failure_is_bounded(cap_logs, monkeypatch):
    _patch_alembic(monkeypatch, heads=["001"], revisions=[_fake_revision("001")])
    _set_database_url(monkeypatch, "mysql+aiomysql://u:p@h/dbname")
    monkeypatch.setattr(migrate, "CONNECT_BACKOFF_BASE_SECONDS", 0)

    calls = {"n": 0}

    def _always_down(_url):
        calls["n"] += 1
        raise _FakeDriverError(2003, "Can't connect")

    monkeypatch.setattr(migrate, "_get_current_revision_sync", _always_down)

    rc = migrate.main()

    assert rc == 1
    assert calls["n"] == migrate.CONNECT_MAX_ATTEMPTS
    assert any(e["event"] == "migrate.connect.giving_up" for e in cap_logs.entries)


def test_failure_event_carries_the_errno_but_never_the_driver_message(
    cap_logs, monkeypatch
):
    """⚠ The whole reason this ticket exists: the record must name the cause
    WITHOUT leaking the credential-bearing message."""
    _patch_alembic(monkeypatch, heads=["001"], revisions=[_fake_revision("001")])
    _set_database_url(monkeypatch, "mysql+aiomysql://pfv_app:s3cret@10.108.0.3/dbname")
    monkeypatch.setattr(migrate, "CONNECT_BACKOFF_BASE_SECONDS", 0)

    secret_message = "Access denied for user 'pfv_app'@'10.108.0.3' (using password: YES)"
    monkeypatch.setattr(
        migrate,
        "_get_current_revision_sync",
        lambda _url: (_ for _ in ()).throw(_FakeDriverError(1045, secret_message)),
    )

    rc = migrate.main()
    assert rc == 1

    blob = repr(cap_logs.entries)
    assert "1045" in blob, "the errno must be present - it is the diagnostic"
    for leak in ("s3cret", "pfv_app", "10.108.0.3", "using password"):
        assert leak not in blob, f"{leak!r} leaked into the structured log"


def test_retry_does_not_extend_to_the_alembic_upgrade(cap_logs, monkeypatch):
    """⚠ SCOPE FENCE. Retrying a failed migration is a different and far more
    dangerous thing than retrying a connect: the wrapper drives alembic per
    revision so a failure stops at a KNOWN revision, and silently re-running one
    would undo that guarantee.

    ⚠⚠ THIS FENCE PATCHES `subprocess.Popen`, NOT `_run_alembic_upgrade`.
    The first version patched `_run_alembic_upgrade` and was VACUOUS: a mutant
    that renamed the real function and wrapped it in a 3x retry loop left this
    test GREEN, because monkeypatching the outer name replaced the very loop
    being tested. Counting Popen invocations sees retries introduced at ANY
    layer above the subprocess. Do not "simplify" this back.
    """
    revs = [_fake_revision("001"), _fake_revision("002")]
    _patch_alembic(monkeypatch, heads=["002"], revisions=revs)
    _set_database_url(monkeypatch, "mysql+aiomysql://u:p@h/dbname")
    monkeypatch.setattr(migrate, "CONNECT_BACKOFF_BASE_SECONDS", 0)
    monkeypatch.setattr(migrate, "_get_current_revision_sync", lambda _url: "001")

    popens = {"n": 0}

    class _FailingProc:
        """A subprocess that exits non-zero with no output."""

        def __init__(self, *_a, **_kw):
            popens["n"] += 1
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("")

        def wait(self) -> int:
            return 9

        def poll(self) -> int:
            return 9

        @property
        def returncode(self) -> int:
            return 9

    monkeypatch.setattr(migrate.subprocess, "Popen", _FailingProc)

    rc = migrate.main()

    assert rc == 9
    assert popens["n"] == 1, (
        f"alembic was invoked {popens['n']} times for a failing revision; a "
        "failed migration must be attempted EXACTLY ONCE. A retry here can "
        "re-run a partially-applied revision."
    )
