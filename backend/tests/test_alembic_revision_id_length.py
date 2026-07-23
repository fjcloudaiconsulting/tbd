"""Regression guard: every Alembic revision id fits ``VARCHAR(32)``.

Alembic stores the current head's ``revision`` string in
``alembic_version.version_num``, whose column type is ``VARCHAR(32)`` (the
Alembic default; PFV does not override ``version_table`` or its width).
A revision id longer than 32 characters is a production landmine that
SQLite CI does not catch:

  On MySQL, ``alembic upgrade`` applies the migration's DDL *first*, then
  runs ``UPDATE alembic_version SET version_num = '<revision>'``. When the
  id exceeds 32 chars that UPDATE fails on the column-width constraint —
  so the schema change lands but the version stamp does not, leaving the
  database in a half-migrated state that the next deploy cannot recover
  cleanly. SQLite's ``version_num`` is untyped text, so the same migration
  passes CI green. This exact failure shipped once (a 38-char id); see
  ``reference_cc_statement_alerts`` (migration 076, kept to 25 chars) and
  the durable GOTCHA in ``project_status``.

Why an AST walk rather than a ``grep`` in CI: the ``revision`` id is a
module-level assignment (``revision: str = "..."``); a text scan would
also trip on ``down_revision`` and on the word "revision" in docstrings.
The AST reads only the real assignment node, pins the offending file, and
runs in the existing pytest shards — no separate CI job, and developers
hit it locally before pushing. Modelled on
``tests/test_no_raw_request_client.py``, the established source-guard
pattern.

If Alembic's ``version_num`` width is ever widened (a deliberate
migration of the ``alembic_version`` table), update
:data:`MAX_REVISION_ID_LENGTH` to match — do not raise it to dodge a long
id, since the ceiling mirrors the real column.
"""
from __future__ import annotations

import ast
from pathlib import Path


# ``alembic_version.version_num`` is ``VARCHAR(32)`` (Alembic default).
MAX_REVISION_ID_LENGTH = 32

VERSIONS_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _module_revision(tree: ast.Module) -> str | None:
    """Return the module-level ``revision`` string literal, or ``None``.

    Handles both the annotated form Alembic's template emits
    (``revision: str = "..."`` → :class:`ast.AnnAssign`) and a plain
    ``revision = "..."`` (:class:`ast.Assign`). Only a direct string
    constant counts; a computed value would (correctly) read as absent and
    fail the "every file has a revision" assertion below.
    """
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "revision"
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "revision" for t in node.targets
        ):
            if isinstance(node.value, ast.Constant) and isinstance(
                node.value.value, str
            ):
                return node.value.value
    return None


def _revision_files() -> list[Path]:
    """Every migration module under ``alembic/versions`` (skips dunders)."""
    return sorted(
        p
        for p in VERSIONS_DIR.glob("*.py")
        if not p.name.startswith("__")
    )


def test_every_alembic_revision_id_fits_version_num_column():
    """No migration's ``revision`` id exceeds ``VARCHAR(32)``.

    Two assertions, separated so a failure points cleanly:

      * Every version file exposes a string ``revision`` literal (a parse
        or refactor that hides it would otherwise silently skip the
        length check).
      * Every ``revision`` id is at most 32 characters — the width of
        ``alembic_version.version_num`` on MySQL/prod.
    """
    files = _revision_files()
    assert files, f"no Alembic version files found under {VERSIONS_DIR}"

    revisions: list[tuple[str, str]] = []  # (filename, revision)
    missing: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rev = _module_revision(tree)
        if rev is None:
            missing.append(path.name)
        else:
            revisions.append((path.name, rev))

    assert not missing, (
        "Alembic version file(s) with no string `revision` assignment — the "
        "length guard cannot inspect them:\n"
        + "\n".join(f"  - {name}" for name in missing)
    )

    too_long = [
        (name, rev) for name, rev in revisions if len(rev) > MAX_REVISION_ID_LENGTH
    ]
    assert not too_long, (
        f"Alembic revision id(s) longer than {MAX_REVISION_ID_LENGTH} chars — "
        "they overflow `alembic_version.version_num VARCHAR(32)` and half-apply "
        "on MySQL while passing SQLite CI (see reference_cc_statement_alerts):\n"
        + "\n".join(
            f"  - {name}: {rev!r} ({len(rev)} chars)"
            for name, rev in sorted(too_long, key=lambda r: -len(r[1]))
        )
    )
