"""Add is_system/slug to account_types, close_day to accounts

Revision ID: 007
Revises: 006
Create Date: 2026-03-31

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "account_types",
        sa.Column("slug", sa.String(50), nullable=True),
    )
    op.add_column(
        "account_types",
        sa.Column(
            "is_system",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "accounts",
        sa.Column("close_day", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("accounts", "close_day")
    op.drop_column("account_types", "is_system")
    op.drop_column("account_types", "slug")
