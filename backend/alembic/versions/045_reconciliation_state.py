"""Add reconciliation state machine + import_batches table (L3.2 Wave 2B).

Revision ID: 045_reconciliation_state
Revises: 044_feedback_entries
Create Date: 2026-05-13

Rebased onto main 2026-05-13: chains off ``044_feedback_entries``
(PR #250) instead of the original ``042_users_onboarded_at`` head.
The migration body is unchanged; only the revision string + parent
pointer move.

Implements the persistence half of the Reconciliation State Machine
contract (``specs/2026-05-12-l3-2-import-contracts.md`` §3 and §3.2).

New table ``import_batches`` (header):

    id                  PRIMARY KEY
    org_id              FK -> organizations.id
    account_id          FK -> accounts.id
    source_format       ENUM('csv','ofx')
    file_name           VARCHAR(255)
    created_at          DATETIME
    created_by_user_id  FK -> users.id
    status              ENUM('open','closed') DEFAULT 'open'
    row_count           INT
    accepted_count      INT
    pending_count       INT
    closed_at           DATETIME NULL

Three new columns on ``transactions``:

    import_batch_id        BIGINT NULL FK -> import_batches.id ON DELETE SET NULL
    reconciliation_state   ENUM(...) NOT NULL DEFAULT 'accepted'
    fitid                  VARCHAR(255) NULL

Backfill (§3.2.2, CANONICAL):

    Every existing transaction (especially ``is_imported = True`` rows)
    must land at ``reconciliation_state = 'accepted'``. The column-level
    ``NOT NULL DEFAULT 'accepted'`` does the work on MySQL 8 at column-add
    time, but a defensive post-DDL UPDATE backstops the contract on any
    dialect where the engine fills the new column with NULL on existing
    rows.

Indexes:

    transactions(import_batch_id, reconciliation_state) — drives the
    reconciliation inbox's "rows in this batch grouped by state" query.

    transactions(org_id, fitid) — OFX cross-batch dedup. The org scope
    is leading so we never scan another org's space when looking up a
    bank FITID.

Post-upgrade invariant (regression-tested in
``backend/tests/migrations/test_045_reconciliation_state.py``):

    SELECT COUNT(*) FROM transactions WHERE reconciliation_state IS NULL = 0

Forward-compatibility note: chains off ``042_users_onboarded_at``, the
current head as of 2026-05-13.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "045_reconciliation_state"
down_revision: Union[str, None] = "044_feedback_entries"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Enum value lists (lower-case, matching the project's
# ``values_callable=lambda x: [e.value for e in x]`` convention).
_RECON_STATES = (
    "pending_review",
    "matched",
    "unmatched",
    "skipped",
    "edited",
    "accepted",
    "rejected",
)
_SOURCE_FORMATS = ("csv", "ofx")
_BATCH_STATUSES = ("open", "closed")


def upgrade() -> None:
    # ── (1) import_batches table ──
    op.create_table(
        "import_batches",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "org_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("accounts.id"),
            nullable=False,
        ),
        sa.Column(
            "source_format",
            sa.Enum(*_SOURCE_FORMATS, name="import_source_format"),
            nullable=False,
        ),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(*_BATCH_STATUSES, name="import_batch_status"),
            nullable=False,
            server_default="open",
        ),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "accepted_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "pending_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "idx_import_batches_org_created",
        "import_batches",
        ["org_id", "created_at"],
    )

    # ── (2) transactions.import_batch_id (nullable FK) ──
    op.add_column(
        "transactions",
        sa.Column("import_batch_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_transactions_import_batch_id",
        "transactions",
        "import_batches",
        ["import_batch_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ── (3) transactions.reconciliation_state ──
    op.add_column(
        "transactions",
        sa.Column(
            "reconciliation_state",
            sa.Enum(*_RECON_STATES, name="transaction_reconciliation_state"),
            nullable=False,
            server_default="accepted",
        ),
    )

    # ── (4) transactions.fitid ──
    op.add_column(
        "transactions",
        sa.Column("fitid", sa.String(length=255), nullable=True),
    )

    # ── (5) Defensive backfill (§3.2.2) ──
    # The DDL above already fills the column with 'accepted' at column-add
    # time on MySQL 8, but the contract requires an explicit UPDATE to
    # cover edge cases where the engine leaves the column NULL on some
    # pre-existing row. The WHERE clause is a no-op when the DDL filled
    # the column correctly.
    op.execute(
        "UPDATE transactions SET reconciliation_state = 'accepted' "
        "WHERE reconciliation_state IS NULL"
    )

    # ── (6) Indexes on transactions ──
    op.create_index(
        "idx_transactions_import_batch_state",
        "transactions",
        ["import_batch_id", "reconciliation_state"],
    )
    op.create_index(
        "idx_transactions_org_fitid",
        "transactions",
        ["org_id", "fitid"],
    )


def downgrade() -> None:
    # MySQL refuses to drop the composite ``(import_batch_id,
    # reconciliation_state)`` index while the FK on ``import_batch_id``
    # is using it for its auto-cover (errno 1553 -- the
    # MySQL FK Index Cover trap; see reference_mysql_fk_index_cover.md).
    # We drop the FK first so the index is no longer needed.
    #
    # Each operation is guarded with an information_schema check so the
    # downgrade is idempotent across partial-prior-runs: DDL on MySQL is
    # auto-commit (no transactional rollback), so a half-finished
    # downgrade can leave individual objects already missing. The guards
    # make a re-run safe.
    bind = op.get_bind()

    def _fk_exists(constraint_name: str) -> bool:
        return bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM information_schema.referential_constraints "
                "WHERE CONSTRAINT_SCHEMA = DATABASE() "
                "AND TABLE_NAME = 'transactions' "
                "AND CONSTRAINT_NAME = :name"
            ),
            {"name": constraint_name},
        ).scalar()

    def _index_exists(table: str, index_name: str) -> bool:
        return bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM information_schema.statistics "
                "WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = :table "
                "AND INDEX_NAME = :name"
            ),
            {"table": table, "name": index_name},
        ).scalar()

    def _column_exists(table: str, column: str) -> bool:
        return bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = :table "
                "AND COLUMN_NAME = :column"
            ),
            {"table": table, "column": column},
        ).scalar()

    def _table_exists(table: str) -> bool:
        return bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = :table"
            ),
            {"table": table},
        ).scalar()

    if _fk_exists("fk_transactions_import_batch_id"):
        op.drop_constraint(
            "fk_transactions_import_batch_id",
            "transactions",
            type_="foreignkey",
        )
    if _index_exists("transactions", "idx_transactions_import_batch_state"):
        op.drop_index(
            "idx_transactions_import_batch_state", table_name="transactions"
        )
    if _index_exists("transactions", "idx_transactions_org_fitid"):
        op.drop_index(
            "idx_transactions_org_fitid", table_name="transactions"
        )
    if _column_exists("transactions", "fitid"):
        op.drop_column("transactions", "fitid")
    if _column_exists("transactions", "reconciliation_state"):
        op.drop_column("transactions", "reconciliation_state")
    if _column_exists("transactions", "import_batch_id"):
        op.drop_column("transactions", "import_batch_id")
    # ``idx_import_batches_org_created`` covers the FK on ``org_id``,
    # so MySQL refuses an explicit DROP INDEX (errno 1553). Dropping the
    # table is the right move -- it tears down every dependent object in
    # one shot, including the auto-cover index.
    if _table_exists("import_batches"):
        op.drop_table("import_batches")
    # Named enums on MySQL are stored inline on the column, so dropping
    # the column drops the enum. No separate ``Enum.drop()`` needed.
