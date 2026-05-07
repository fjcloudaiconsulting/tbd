"""Migration timestamp-default portability test.

The reviewer for PRs #139 and #142 ran `alembic upgrade head` against
SQLite (e.g. for local exploration) and migrations 030 and 033 blew
up on `sa.text("CURRENT_TIMESTAMP(6)")` (MySQL-only literal syntax —
SQLite parses it as a function call missing a `(` and rejects).

The codebase's dominant pattern is `sa.func.now()`, which Alembic
translates per dialect (`CURRENT_TIMESTAMP` on SQLite, `NOW()` on
MySQL). This test grep-asserts that no migration smuggles back a
literal `CURRENT_TIMESTAMP(...)` text default — that's the only
portability issue we know about, and it's a one-line drift away
from regressing.

We don't claim SQLite is a supported runtime — production is MySQL.
But migration files must compile on SQLite so reviewers and local
exploration tools work. Migrations 008+ already use `op.add_column`
which doesn't ALTER constraints, so the file-level ban here is the
appropriate guard, not a full `alembic upgrade head` smoke run.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


VERSIONS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "versions"

# Pattern: sa.text("CURRENT_TIMESTAMP(<digits>)") — SQLite rejects the
# parenthesised precision. Catches both single- and double-quoted
# literals.
_CURRENT_TIMESTAMP_PRECISION_RE = re.compile(
    r"""sa\.text\(\s*['"]CURRENT_TIMESTAMP\(\d+\)['"]\s*\)""",
)


@pytest.mark.parametrize(
    "migration_file",
    sorted(p for p in VERSIONS_DIR.glob("*.py") if not p.name.startswith("__")),
    ids=lambda p: p.name,
)
def test_no_mysql_only_current_timestamp_precision_default(migration_file):
    """No migration may use `sa.text("CURRENT_TIMESTAMP(N)")` as a
    server_default — it parses as MySQL-only and breaks SQLite.
    Use `sa.func.now()` instead; it dialect-translates."""
    src = migration_file.read_text()
    matches = _CURRENT_TIMESTAMP_PRECISION_RE.findall(src)
    assert not matches, (
        f"{migration_file.name} uses MySQL-only "
        f"`CURRENT_TIMESTAMP(N)` literal text default. Replace with "
        f"`sa.func.now()` for dialect portability. Found: {matches!r}"
    )
