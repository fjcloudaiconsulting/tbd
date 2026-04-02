"""Add recurring_id to transactions

Revision ID: 012
Revises: 011
Create Date: 2026-04-02

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column(
            "recurring_id",
            sa.Integer(),
            sa.ForeignKey("recurring_transactions.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_transactions_recurring", "transactions", ["recurring_id"])


def downgrade() -> None:
    op.drop_index("ix_transactions_recurring")
    op.drop_column("transactions", "recurring_id")
