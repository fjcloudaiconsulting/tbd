"""Migration 041 — opening_balance + opening_balance_date.

Asserts the CANONICAL contract-locked invariant from
``specs/2026-05-12-l3-2-import-contracts.md`` §4.4:

    For existing accounts, the migration backfills
    ``opening_balance = 0`` for every row.

We run the assertion as a live SQL probe against the post-upgrade
schema. The Wave 2A Opening Balance team owns this gate; any future
migration that violates it (by inserting a non-zero opening_balance
on a pre-existing row before the column was introduced, or by
changing the DDL default) must update this file too.

Skipped unless ``PFV_RUN_MYSQL_TESTS=1`` because the assertion is
against the real post-migration MySQL state. The default DATABASE_URL
is MySQL even in environments where no MySQL is running, so an
explicit opt-in is the only reliable signal.
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
async def test_opening_balance_backfill_is_zero_for_every_account():
    """§4.4: ``SELECT COUNT(*) FROM accounts WHERE opening_balance != 0``
    MUST equal 0 immediately after upgrade. This proves the canonical
    backfill landed for every pre-existing account."""
    async for db in get_db():
        result = await db.execute(
            text("SELECT COUNT(*) AS n FROM accounts WHERE opening_balance != 0")
        )
        row = result.first()
        assert row.n == 0, (
            f"§4.4 backfill regression: {row.n} accounts have a non-zero "
            "opening_balance immediately after migration. Every existing "
            "account must be backfilled to 0; users set the real value via "
            "the Opening Balance UI."
        )
        break


@pytest.mark.asyncio
async def test_opening_balance_columns_have_correct_types():
    """Schema-level sanity: the two columns landed with the contract's
    exact types (DECIMAL(12,2) and DATE) and are NOT NULL. The contract
    pins the shape so the OFX + Reconciliation teams can rely on it."""
    async for db in get_db():
        result = await db.execute(
            text(
                "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, "
                "       NUMERIC_PRECISION, NUMERIC_SCALE "
                "FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = 'accounts' "
                "AND COLUMN_NAME IN ('opening_balance', 'opening_balance_date')"
            )
        )
        rows = {r.COLUMN_NAME: r for r in result.all()}
        assert "opening_balance" in rows
        assert "opening_balance_date" in rows

        ob = rows["opening_balance"]
        assert ob.DATA_TYPE == "decimal"
        assert ob.IS_NULLABLE == "NO"
        assert ob.NUMERIC_PRECISION == 12
        assert ob.NUMERIC_SCALE == 2

        obd = rows["opening_balance_date"]
        assert obd.DATA_TYPE == "date"
        assert obd.IS_NULLABLE == "NO"
        break
