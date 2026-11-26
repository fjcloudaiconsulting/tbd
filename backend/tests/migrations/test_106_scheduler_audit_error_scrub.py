"""Exercise the audit scrub on SQLite and optionally on disposable MySQL 8.4.

Set SCHEDULER_AUDIT_MYSQL_URL to the disposable database used for Alembic.
The test uses a connection-local temporary table on both dialects.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

MYSQL_URL = os.environ.get("SCHEDULER_AUDIT_MYSQL_URL")
_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "106_scheduler_audit_error_scrub.py"
_spec = importlib.util.spec_from_file_location("_m106", _PATH)
m106 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m106)


def _stored_rows(conn):
    return {row.id: tuple(row) for row in conn.execute(text("SELECT * FROM audit_events ORDER BY id"))}


def _check_scrub(conn):
    conn.execute(text(
        "CREATE TEMPORARY TABLE audit_events ("
        "id BIGINT PRIMARY KEY, event_type VARCHAR(80) NOT NULL, outcome VARCHAR(7) NOT NULL, detail JSON)"
    ))
    failure = "scheduler.job.failure"
    rows = [
        (1, failure, "failure", '{ "error": "(pkg.errors.DatabaseError) SECRET", "attempt": 3 }'),
        (2, "scheduler.billing.close.failure", "failure", '{ "error": "boom SECRET here", "data": {"steps": [1, 2]} }'),
        (3, failure, "failure", '{ "reason": "unchanged" }'),
        (4, failure, "failure", None),
        (7, failure, "failure", '{ "error": "details (app.Error) SECRET", "trace": null }'),
        (8, "admin.org.delete.failed", "failure", '{ "error": "boom SECRET here", "note": "keep formatting" }'),
        (9, "scheduler.job.success", "success", '{ "error": "boom SECRET here", "note": "keep formatting" }'),
        (10, "Scheduler.job.failure", "failure", '{ "error": "boom SECRET here" }'),
        (11, failure, "failure", '{ "error": {"nested": "SECRET"} }'),
        (12, failure, "failure", '[ "error", "unchanged" ]'),
        (13, failure, "failure", '{ "error": "OperationalError" }'),
        (14, failure, "failure", '{ "error": "OperationalError errno=1213" }'),
        (15, failure, "failure", json.dumps({
            "error": "(pymysql.err.IntegrityError) (1062, \"Duplicate entry 'a@b.c'\")",
        })),
        (16, failure, "failure", json.dumps({"error": f"(pkg.{'A' * 95}SECRET) raw error"})),
    ]
    conn.execute(
        text("INSERT INTO audit_events (id, event_type, outcome, detail) VALUES (:id, :type, :outcome, :detail)"),
        [{"id": row_id, "type": kind, "outcome": outcome, "detail": detail} for row_id, kind, outcome, detail in rows],
    )
    before = _stored_rows(conn)
    with Operations.context(MigrationContext.configure(conn)):
        m106.upgrade()
        after = _stored_rows(conn)
        m106.downgrade()

    expected = {
        1: "DatabaseError",
        2: "redacted",
        7: "redacted",
        11: "redacted",
        15: "IntegrityError errno=1062",
        16: "redacted",
    }
    assert after.keys() == before.keys()
    for row_id, before_row in before.items():
        after_row = after[row_id]
        if row_id in expected:
            assert after_row[:-1] == before_row[:-1]
            assert "SECRET" not in after_row[-1]
            assert json.loads(after_row[-1]) == {**json.loads(before_row[-1]), "error": expected[row_id]}
        else:
            assert after_row == before_row
    assert _stored_rows(conn) == after


@pytest.mark.parametrize("url", [
    pytest.param("sqlite+aiosqlite://", id="sqlite"),
    pytest.param(MYSQL_URL, id="mysql", marks=pytest.mark.skipif(not MYSQL_URL, reason="SCHEDULER_AUDIT_MYSQL_URL not set")),
])
async def test_scrub_scheduler_failures(url, monkeypatch):
    monkeypatch.setattr(m106, "BATCH", 2)
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(_check_scrub)
    finally:
        await engine.dispose()
