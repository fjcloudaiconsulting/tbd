"""Migration 098 (TBD-598): the three ``scenarios`` FKs go in one ALTER.

Renders the real upgrade and downgrade as MySQL SQL (alembic offline mode),
so no database is needed. The live up/down run on MySQL 8.4 is a merge gate.
"""
from __future__ import annotations

import importlib.util
import io
import re
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations

_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "098_plans_schema.py"
_spec = importlib.util.spec_from_file_location("_m098", _PATH)
m098 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m098)


def _render(fn) -> list[str]:
    buf = io.StringIO()
    ctx = MigrationContext.configure(
        dialect_name="mysql", opts={"as_sql": True, "output_buffer": buf}
    )
    with Operations.context(ctx):
        fn()
    return [s.strip() for s in buf.getvalue().split(";") if s.strip()]


def _touching_fks(statements: list[str]) -> list[str]:
    return [s for s in statements if re.search(r"FOREIGN KEY fk_scenarios_|CONSTRAINT fk_scenarios_", s)]


def test_upgrade_adds_the_scenarios_fks_in_one_alter():
    """FENCE. Wrong implementation: one ``create_foreign_key`` per column,
    which makes MySQL copy ``scenarios`` three times."""
    (stmt,) = _touching_fks(_render(m098.upgrade))
    assert stmt.startswith("ALTER TABLE scenarios ")
    for column, table in m098._SCENARIO_FKS:
        assert (
            f"ADD CONSTRAINT fk_scenarios_{column} FOREIGN KEY ({column}) "
            f"REFERENCES {table} (id) ON DELETE SET NULL"
        ) in stmt


def test_downgrade_drops_the_scenarios_fks_in_one_alter():
    (stmt,) = _touching_fks(_render(m098.downgrade))
    assert stmt.startswith("ALTER TABLE scenarios ")
    assert stmt.count("DROP FOREIGN KEY fk_scenarios_") == len(m098._SCENARIO_FKS)
