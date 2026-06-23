"""Add ix_transactions_org_type composite index.

Revision ID: 067_ix_transactions_org_type
Revises: 066_founder_fields
Create Date: 2026-06-23

Covers the Sankey builder's default (no-date-filter) full-org scan:
SELECT … FROM transactions WHERE org_id = ? AND type = ? GROUP BY category.
Without this index the query does an org_id-only range scan then filters
type in the engine.  The composite covers both predicates in one pass.
"""
from __future__ import annotations

from alembic import op

revision = "067_ix_transactions_org_type"
down_revision = "066_founder_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_transactions_org_type",
        "transactions",
        ["org_id", "type"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_transactions_org_type",
        table_name="transactions",
    )
