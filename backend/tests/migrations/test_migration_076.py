"""Migration 076 -- cc_statement notification category + prefs.

Asserts the post-migration MySQL schema shape for CC Statement Alerts
V1 Task 1:

1. ``notifications.category`` is a native MySQL ENUM widened to include
   ``'cc_statement'`` alongside the four existing values.
2. ``user_notification_preferences`` gained ``email_cc_statement`` /
   ``in_app_cc_statement``, both ``NOT NULL`` with a ``1`` default so
   existing rows backfill to opt-out-only (default ON).

Skipped unless ``PFV_RUN_MYSQL_TESTS=1`` because the assertion is
against the real post-migration MySQL state (SQLite has no native
ENUM and cannot exercise ``ALTER ... MODIFY ENUM``), mirroring
``test_045_reconciliation_state.py``.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from app.database import get_db


pytestmark = pytest.mark.skipif(
    os.environ.get("PFV_RUN_MYSQL_TESTS") != "1",
    reason="MySQL-only migration test; set PFV_RUN_MYSQL_TESTS=1 to run.",
)


@pytest.mark.asyncio
async def test_notifications_category_enum_includes_cc_statement():
    async for db in get_db():
        result = await db.execute(
            text("SHOW COLUMNS FROM notifications LIKE 'category'")
        )
        row = result.first()
        assert row is not None, "notifications.category column not found"
        col_type = row.Type if hasattr(row, "Type") else row[1]
        assert "cc_statement" in col_type, (
            f"expected 'cc_statement' in the notifications.category ENUM, "
            f"got: {col_type!r}"
        )
        for existing in ("security", "account", "org_admin", "org_activity"):
            assert existing in col_type, (
                f"migration 076 must WIDEN the enum, not replace it — "
                f"missing pre-existing value {existing!r} in {col_type!r}"
            )
        break


@pytest.mark.asyncio
async def test_preference_columns_exist_not_null_default_on():
    async for db in get_db():
        result = await db.execute(
            text(
                "SELECT COLUMN_NAME, IS_NULLABLE, COLUMN_DEFAULT "
                "FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = 'user_notification_preferences' "
                "AND COLUMN_NAME IN ('email_cc_statement', 'in_app_cc_statement')"
            )
        )
        rows = {r.COLUMN_NAME: r for r in result.all()}

        assert "email_cc_statement" in rows
        assert rows["email_cc_statement"].IS_NULLABLE == "NO"
        assert rows["email_cc_statement"].COLUMN_DEFAULT == "1"

        assert "in_app_cc_statement" in rows
        assert rows["in_app_cc_statement"].IS_NULLABLE == "NO"
        assert rows["in_app_cc_statement"].COLUMN_DEFAULT == "1"
        break


@pytest.mark.asyncio
async def test_existing_preference_rows_backfilled_to_default_on():
    """Any preference row that predates migration 076 must have both
    new columns land TRUE (opt-out, not opt-in) — the ``server_default
    '1'`` is what MySQL applies to existing rows on ``ADD COLUMN``.
    """
    async for db in get_db():
        result = await db.execute(
            text(
                "SELECT COUNT(*) AS n FROM user_notification_preferences "
                "WHERE email_cc_statement != 1 OR in_app_cc_statement != 1"
            )
        )
        row = result.first()
        assert row.n == 0, (
            f"{row.n} preference rows have cc_statement OFF immediately "
            "after migration 076 — the ADD COLUMN server_default should "
            "leave every existing row opted IN."
        )
        break
