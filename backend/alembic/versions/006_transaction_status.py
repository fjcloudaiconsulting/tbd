"""Add status column to transactions

Revision ID: 006
Revises: 005
Create Date: 2026-03-31

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column(
            "status",
            sa.Enum("settled", "pending", name="transactionstatus"),
            nullable=False,
            server_default="settled",
        ),
    )
    op.create_index("ix_transactions_status", "transactions", ["status"])


def downgrade() -> None:
    op.drop_index("ix_transactions_status")
    op.drop_column("transactions", "status")

    # Drop the enum type (needed for PostgreSQL; no-op on MySQL)
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        sa.Enum(name="transactionstatus").drop(bind, checkfirst=True)
