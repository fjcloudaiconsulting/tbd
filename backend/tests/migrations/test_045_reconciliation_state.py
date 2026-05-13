"""Migration 045 -- reconciliation_state + import_batches.

Asserts the CANONICAL contract-locked invariants from
``specs/2026-05-12-l3-2-import-contracts.md`` §3.2 / §3.2.2:

1. Every existing transaction row (especially ``is_imported=True``) must
   land at ``reconciliation_state = 'accepted'`` after migration. Without
   this backfill, historical imports would retroactively appear in the
   review inbox the moment the column ships -- a direct contradiction of
   Decision 3.
2. The new columns and FK + indexes exist with the correct shape.
3. The ``import_batches`` table exists with the correct columns.

Skipped unless ``PFV_RUN_MYSQL_TESTS=1`` because the assertion is
against the real post-migration MySQL state.
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
async def test_reconciliation_state_backfilled_to_accepted_for_every_row():
    """§3.2.2: every pre-existing transaction lands ``ACCEPTED`` after
    migration. The post-DDL UPDATE backstops the column-level
    ``DEFAULT 'accepted'`` in case any dialect leaves the new column NULL
    on existing rows."""
    async for db in get_db():
        # No NULLs anywhere.
        null_result = await db.execute(
            text(
                "SELECT COUNT(*) AS n FROM transactions "
                "WHERE reconciliation_state IS NULL"
            )
        )
        null_row = null_result.first()
        assert null_row.n == 0, (
            f"§3.2.2 backfill regression: {null_row.n} transactions have "
            "a NULL reconciliation_state immediately after migration. The "
            "DDL DEFAULT 'accepted' + post-DDL UPDATE should leave zero."
        )

        # And no non-accepted rows (since the only source is the
        # migration backfill itself; service code may flip rows later).
        non_accepted = await db.execute(
            text(
                "SELECT COUNT(*) AS n FROM transactions "
                "WHERE reconciliation_state != 'accepted'"
            )
        )
        non_accepted_row = non_accepted.first()
        assert non_accepted_row.n == 0, (
            f"§3.2.2 backfill regression: {non_accepted_row.n} pre-"
            "existing transactions are NOT in the 'accepted' state. The "
            "backfill must leave every row 'accepted'; the recon UI may "
            "flip rows only after the migration completes."
        )
        break


@pytest.mark.asyncio
async def test_transaction_columns_have_correct_shape():
    """Schema-level sanity check on the three new columns."""
    async for db in get_db():
        result = await db.execute(
            text(
                "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT "
                "FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = 'transactions' "
                "AND COLUMN_NAME IN "
                "  ('import_batch_id', 'reconciliation_state', 'fitid')"
            )
        )
        rows = {r.COLUMN_NAME: r for r in result.all()}

        assert "import_batch_id" in rows
        assert rows["import_batch_id"].IS_NULLABLE == "YES"

        assert "reconciliation_state" in rows
        assert rows["reconciliation_state"].IS_NULLABLE == "NO"
        # Column default surfaces as 'accepted' on MySQL.
        assert rows["reconciliation_state"].COLUMN_DEFAULT == "accepted"

        assert "fitid" in rows
        assert rows["fitid"].IS_NULLABLE == "YES"
        assert rows["fitid"].DATA_TYPE == "varchar"
        break


@pytest.mark.asyncio
async def test_import_batches_table_exists():
    """The new ``import_batches`` table is present with the expected
    header + counter columns."""
    async for db in get_db():
        result = await db.execute(
            text(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = 'import_batches'"
            )
        )
        names = {r.COLUMN_NAME for r in result.all()}
        expected = {
            "id",
            "org_id",
            "account_id",
            "source_format",
            "file_name",
            "created_at",
            "created_by_user_id",
            "status",
            "row_count",
            "accepted_count",
            "pending_count",
            "closed_at",
        }
        missing = expected - names
        assert not missing, (
            f"import_batches missing columns: {sorted(missing)}"
        )
        break


@pytest.mark.asyncio
async def test_recon_indexes_exist():
    """Indexes the recon UI relies on: (import_batch_id, state) for
    per-batch grouping, (org_id, fitid) for OFX cross-batch dedup."""
    async for db in get_db():
        result = await db.execute(
            text(
                "SELECT INDEX_NAME FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = 'transactions' "
                "AND INDEX_NAME IN ("
                "  'idx_transactions_import_batch_state',"
                "  'idx_transactions_org_fitid'"
                ")"
            )
        )
        names = {r.INDEX_NAME for r in result.all()}
        assert "idx_transactions_import_batch_state" in names
        assert "idx_transactions_org_fitid" in names
        break
