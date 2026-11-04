"""Migration 100 (TBD-601): plan_goals.tier and kind become VARCHAR(32).

Renders the real upgrade as MySQL SQL (alembic offline mode), and runs the
downgrade's refusal on SQLite. The live up/down run on MySQL 8.4 is a merge gate.
"""
from __future__ import annotations

import importlib.util
import io
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "100_plan_goal_varchar.py"
_spec = importlib.util.spec_from_file_location("_m100", _PATH)
m100 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m100)


def _render(fn) -> list[str]:
    buf = io.StringIO()
    ctx = MigrationContext.configure(
        dialect_name="mysql", opts={"as_sql": True, "output_buffer": buf}
    )
    with Operations.context(ctx):
        fn()
    return [s.strip() for s in buf.getvalue().split(";") if s.strip()]


def test_upgrade_makes_both_columns_varchar():
    stmts = _render(m100.upgrade)
    assert stmts == [
        "ALTER TABLE plan_goals MODIFY tier VARCHAR(32) NOT NULL",
        "ALTER TABLE plan_goals MODIFY kind VARCHAR(32) NOT NULL",
    ]


@pytest.mark.parametrize("column,value", [("tier", "nice_to_have"), ("kind", "pension")])
def test_downgrade_refuses_a_value_the_enum_cannot_hold(column, value):
    """FENCE. Wrong implementation: narrowing to the ENUM regardless, which
    MySQL truncates or fails half way."""
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE plan_goals (id INTEGER PRIMARY KEY, tier VARCHAR(32), kind VARCHAR(32))"))
        row = {"tier": "want", "kind": "lump", column: value}
        conn.execute(text("INSERT INTO plan_goals (id, tier, kind) VALUES (1, :tier, :kind)"), row)
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx), pytest.raises(RuntimeError, match=f"plan_goals.{column}"):
            m100.downgrade()
        assert conn.execute(text(f"SELECT {column} FROM plan_goals")).scalar() == value
