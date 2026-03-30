"""Add updated_at to transactions, type to categories

Revision ID: 005
Revises: 004
Create Date: 2026-03-30

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.add_column(
        "categories",
        sa.Column(
            "type",
            sa.Enum("income", "expense", "both", name="categorytype"),
            nullable=False,
            server_default="both",
        ),
    )


def downgrade() -> None:
    op.drop_column("categories", "type")
    op.drop_column("transactions", "updated_at")
