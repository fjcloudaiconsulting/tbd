"""add is_imported to transactions

Revision ID: 019
Revises: 018
"""
from alembic import op
import sqlalchemy as sa

revision = "019"
down_revision = "018"


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column("is_imported", sa.Boolean(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("transactions", "is_imported")
