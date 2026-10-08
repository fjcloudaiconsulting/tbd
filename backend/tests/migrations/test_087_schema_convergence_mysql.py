"""INFRA-129: migration 087 converges prod and fresh schemas, on REAL MySQL.

Run with SCHEMA_CONVERGENCE_MYSQL_URL=mysql+aiomysql://... pointing at a
disposable database already at ``alembic upgrade head`` (the CI ``migrations``
job). 087's own upgrade/downgrade run through an Operations context, so later
revisions are never downgraded. Every test leaves the schema at head.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import re
from pathlib import Path

import pytest
import yaml
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import Base

URL = os.environ.get("SCHEMA_CONVERGENCE_MYSQL_URL")
if not URL and os.environ.get("SCHEMA_CONVERGENCE_MYSQL_REQUIRED") == "1":
    raise RuntimeError("SCHEMA_CONVERGENCE_MYSQL_URL is required here")
mysql = pytest.mark.skipif(not URL, reason="SCHEMA_CONVERGENCE_MYSQL_URL not set")

_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "087_schema_convergence.py"
_spec = importlib.util.spec_from_file_location("_m087", _PATH)
m087 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m087)

TABLES = ("audit_events", "roles", "notifications", "categories", "feedback_entries")


def _col(name: str, fn: str) -> str:
    return f"`{name}` datetime NOT NULL DEFAULT ({fn})"


def _shape(fn: str, org_id_key: bool) -> dict:
    cat = {"PRIMARY KEY (`id`)",
           "UNIQUE KEY `uq_categories_org_slug_system` (`org_id`,`slug`,`is_system`)",
           "KEY `ix_categories_parent_id` (`parent_id`)"}
    if org_id_key:
        cat.add("KEY `org_id` (`org_id`)")
    return {
        "audit_events": {_col("created_at", fn)},
        "roles": {_col("created_at", fn), _col("updated_at", fn)},
        "notifications": {_col("created_at", fn)},
        "categories": cat,
        # Order differs between prod and fresh (cosmetic); the set must not.
        "feedback_entries": {"PRIMARY KEY (`id`)",
                             "KEY `ix_feedback_entries_user_id` (`user_id`)",
                             "KEY `ix_feedback_entries_created_at` (`created_at`)",
                             "KEY `ix_feedback_entries_org_id` (`org_id`)",
                             "KEY `ix_feedback_entries_category` (`category`)"},
    }


CANONICAL = _shape("now()", org_id_key=False)
# Prod's DDL as dumped read-only on 2026-10-08 (INFRA-129).
PROD = _shape("now(6)", org_id_key=True)
PROD_SQL = (
    "ALTER TABLE audit_events ALTER COLUMN created_at SET DEFAULT (now(6))",
    "ALTER TABLE roles ALTER COLUMN created_at SET DEFAULT (now(6)), "
    "ALTER COLUMN updated_at SET DEFAULT (now(6))",
    "ALTER TABLE notifications ALTER COLUMN created_at SET DEFAULT (now(6))",
    "ALTER TABLE categories ADD INDEX org_id (org_id)",
)
_TRACKED = re.compile(r"`(created_at|updated_at)` datetime|KEY ")


def _relevant(sync_conn) -> dict:
    """The drifted parts of SHOW CREATE TABLE: timestamp columns and keys."""
    out = {}
    for t in TABLES:
        ddl = sync_conn.execute(text(f"SHOW CREATE TABLE {t}")).one()[1]
        lines = {ln.strip().rstrip(",") for ln in ddl.splitlines()[1:-1]}
        if t in ("categories", "feedback_entries"):
            out[t] = {ln for ln in lines if "KEY " in ln and "FOREIGN KEY" not in ln}
        else:
            out[t] = {ln for ln in lines if _TRACKED.match(ln)}
    return out


def _run(sync_conn, fn) -> None:
    with Operations.context(MigrationContext.configure(sync_conn)):
        fn()


def _with_conn(fn):
    async def go():
        eng = create_async_engine(URL)
        try:
            async with eng.begin() as conn:
                return await conn.run_sync(fn)
        finally:
            await eng.dispose()
    return asyncio.run(go())


@pytest.fixture
def db():
    yield
    _with_conn(lambda c: _run(c, m087.upgrade))  # always hand back a head schema


@mysql
def test_head_is_canonical(db):
    """FENCE. Wrong implementation: ``op.alter_column(server_default=
    sa.func.now())``, which MySQL stores as ``DEFAULT CURRENT_TIMESTAMP``."""
    assert _with_conn(_relevant) == CANONICAL


@mysql
def test_prod_shape_converges(db):
    """FENCE. Prod's starting state reaches canonical. Wrong implementation:
    upgrade without the index drop (or the notifications alter)."""
    def go(c):
        for stmt in PROD_SQL:
            c.execute(text(stmt))
        before = _relevant(c)
        _run(c, m087.upgrade)
        return before, _relevant(c)
    before, after = _with_conn(go)
    assert before == PROD  # the setup really is prod's drift
    assert after == CANONICAL


@mysql
def test_fresh_shape_converges(db):
    """FENCE. Staging (fresh) has no ``org_id`` index. Wrong implementation:
    an unguarded DROP INDEX (MySQL 1091)."""
    def go(c):
        before = _relevant(c)
        _run(c, m087.upgrade)
        return before, _relevant(c)
    before, after = _with_conn(go)
    assert before == CANONICAL
    assert after == CANONICAL


@mysql
def test_downgrade_restores_prod_then_upgrade_round_trips(db):
    """FENCE. Wrong implementation: a no-op or one-sided downgrade."""
    def go(c):
        _run(c, m087.downgrade)
        down = _relevant(c)
        _run(c, m087.upgrade)
        return down, _relevant(c)
    down, up = _with_conn(go)
    assert down == PROD
    assert up == CANONICAL


@mysql
def test_models_agree_with_migrated_schema(db):
    """FENCE. Autogenerate (server defaults included) sees no diff on these
    tables. Wrong implementation: models left at ``func.now(6)``."""
    def go(c):
        mc = MigrationContext.configure(c, opts={"compare_server_default": True})
        return [d for d in compare_metadata(mc, Base.metadata) if any(t in repr(d) for t in TABLES)]
    assert _with_conn(go) == []


def test_ci_runs_this_module_before_the_last_step():
    """The MySQL tests above skip without a URL; CI must set it, require it,
    and fail on a skip. The last step of the job belongs to TBD-586."""
    root = next(p for p in Path(__file__).resolve().parents
                if (p / ".github/workflows/test.yml").exists())
    steps = yaml.safe_load((root / ".github/workflows/test.yml").read_text())["jobs"]["migrations"]["steps"]
    step = next(s for s in steps[:-1] if Path(__file__).name in s.get("run", ""))
    assert step["env"] == {"SCHEMA_CONVERGENCE_MYSQL_URL": "${{ env.DATABASE_URL }}",
                           "SCHEMA_CONVERGENCE_MYSQL_REQUIRED": "1"}
    assert "pipefail" in step["run"] and "^SKIPPED" in step["run"]
