"""Migration 083 (TBD-585): ``plans.usage_limits`` backfill helper.

Exercises the real ``backfill_usage_limits`` on SQLite. The DDL, the MySQL
``JSON_TYPE`` self-check and the NOT NULL alter are run by ``alembic upgrade
head`` on the MySQL 8.4 stack (recorded in the PR).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.auth.feature_catalog import PlanUsageLimits

_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "083_modular_entitlements.py"
_spec = importlib.util.spec_from_file_location("_m083", _PATH)
m083 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m083)


@pytest.fixture
def conn():
    engine = create_engine("sqlite://")
    with engine.connect() as c:
        c.execute(text("CREATE TABLE plans (id INTEGER PRIMARY KEY, usage_limits JSON)"))
        yield c


def test_the_literal_equals_todays_catalog_default():
    """FENCE F-Q11 (migration half). The migration's literal is the catalog's
    canonical default today. Wrong implementation: a hand-typed dict that
    drifted (a missing meter, an unlimited platform meter)."""
    assert m083.CANONICAL_USAGE_LIMITS == PlanUsageLimits().model_dump(by_alias=True)


def test_backfill_writes_an_object_to_null_rows_only(conn):
    """Wrong implementations: ``json.dumps`` (a JSON string scalar, not an
    object), overwriting a row that already holds limits."""
    kept = {"mcp.calls": {"period": "day", "limit": 3}}
    conn.execute(text("INSERT INTO plans (id, usage_limits) VALUES (1, NULL), (2, NULL)"))
    conn.execute(text("INSERT INTO plans (id, usage_limits) VALUES (3, :v)"),
                 {"v": json.dumps(kept)})
    assert m083.backfill_usage_limits(conn) == 2
    rows = dict(conn.execute(
        text("SELECT id, json_type(usage_limits) FROM plans")).all())
    assert rows == {1: "object", 2: "object", 3: "object"}
    stored = {i: json.loads(v) for i, v in conn.execute(text("SELECT id, usage_limits FROM plans"))}
    assert stored == {1: m083.CANONICAL_USAGE_LIMITS, 2: m083.CANONICAL_USAGE_LIMITS, 3: kept}
    assert m083.backfill_usage_limits(conn) == 0  # idempotent
