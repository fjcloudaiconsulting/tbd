"""Add transfer type and linked_transaction_id

Revision ID: 010
Revises: 009
Create Date: 2026-03-31

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Extend the enum: MySQL requires recreating the column
    op.alter_column(
        "transactions",
        "type",
        type_=sa.Enum("income", "expense", "transfer", name="transactiontype"),
        existing_type=sa.Enum("income", "expense", name="transactiontype"),
        existing_nullable=False,
    )
    op.add_column(
        "transactions",
        sa.Column(
            "linked_transaction_id",
            sa.Integer(),
            sa.ForeignKey("transactions.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_transactions_linked", "transactions", ["linked_transaction_id"])


def downgrade() -> None:
    op.drop_index("ix_transactions_linked")
    op.drop_column("transactions", "linked_transaction_id")
    op.alter_column(
        "transactions",
        "type",
        type_=sa.Enum("income", "expense", name="transactiontype"),
        existing_type=sa.Enum("income", "expense", "transfer", name="transactiontype"),
        existing_nullable=False,
    )
