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
        "UPDATE transactions SET settled_date = date WHERE status = 'settled'"
    )


def downgrade() -> None:
    op.drop_column("transactions", "settled_date")
