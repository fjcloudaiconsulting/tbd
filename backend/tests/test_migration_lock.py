"""``acquire_migration_lock`` (INFRA-83): two migrators must never run DDL at once.

alembic/env.py calls it on the migration connection before running any
revision. Concurrency against a real MySQL is checked by hand (PR record);
these pin the decisions the helper makes.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from app.migration_lock import MigrationLockTimeout, acquire_migration_lock


class _FakeMySQLConnection:
    def __init__(self, get_lock_result):
        self.dialect = SimpleNamespace(name="mysql")
        self.engine = SimpleNamespace(url=SimpleNamespace(database="tbd"))
        self._result = get_lock_result
        self.statements: list[tuple[str, dict]] = []
        self.commits = 0

    def execute(self, statement, params=None):
        self.statements.append((str(statement), params or {}))
        return SimpleNamespace(scalar=lambda: self._result)

    def commit(self):
        self.commits += 1


def test_acquired_lock_commits_so_alembic_opens_its_own_transaction():
    conn = _FakeMySQLConnection(1)
    acquire_migration_lock(conn)
    (sql, params), = conn.statements
    assert "GET_LOCK" in sql
    assert params["name"] == "tbd_migrate.tbd"
    # Without the commit, alembic runs inside the SELECT's implicit
    # transaction and alembic_version is rolled back at close.
    assert conn.commits == 1


@pytest.mark.parametrize("result", [0, None])
def test_lock_not_acquired_refuses_to_migrate(result):
    """0 = another migrator held it for the whole timeout; NULL = error."""
    conn = _FakeMySQLConnection(result)
    with pytest.raises(MigrationLockTimeout):
        acquire_migration_lock(conn)


def test_non_mysql_connection_is_left_alone():
    """sqlite (tests, offline checks) has no GET_LOCK."""
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        acquire_migration_lock(conn)
        assert conn.execute(text("SELECT 1")).scalar() == 1


def test_env_py_locks_before_alembic_runs():
    """The helper is useless unless alembic/env.py calls it first: a real call
    statement in do_run_migrations, ahead of context.configure (a comment or
    string mentioning it does not count)."""
    src = (Path(__file__).resolve().parents[1] / "alembic" / "env.py").read_text()
    fn = next(
        node
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.FunctionDef) and node.name == "do_run_migrations"
    )

    def _called(stmt, name):
        return (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and ast.unparse(stmt.value.func) == name
        )

    order = [
        name
        for stmt in fn.body
        for name in ("acquire_migration_lock", "context.configure")
        if _called(stmt, name)
    ]
    assert order[:2] == ["acquire_migration_lock", "context.configure"]
