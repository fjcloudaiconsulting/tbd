"""add settled_date to transactions

Revision ID: 020
Revises: 019

Adds a nullable settled_date column. For existing settled transactions,
backfills settled_date from the transaction date.
"""
from alembic import op
import sqlalchemy as sa

revision = "020"
down_revision = "019"


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column("settled_date", sa.Date(), nullable=True),
    )
    # Backfill: existing settled transactions get their date as settled_date
    op.execute(
        "UPDATE transactions SET settled_date = `date` WHERE status = 'settled'"
    )
    # Index for budget/forecast queries that filter by org + status + settled_date
    op.create_index(
        "ix_transactions_org_settled_date",
        "transactions",
        ["org_id", "status", "settled_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_transactions_org_settled_date", table_name="transactions")
    op.drop_column("transactions", "settled_date")
