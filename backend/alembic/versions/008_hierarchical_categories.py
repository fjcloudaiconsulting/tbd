"""Add parent_id, description, slug, is_system to categories

Revision ID: 008
Revises: 007
Create Date: 2026-03-31

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "categories",
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("categories.id"), nullable=True),
    )
    op.add_column(
        "categories",
        sa.Column("description", sa.String(255), nullable=True),
    )
    op.add_column(
        "categories",
        sa.Column("slug", sa.String(50), nullable=True),
    )
    op.add_column(
        "categories",
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default="0"),
    )
    op.create_index("ix_categories_parent_id", "categories", ["parent_id"])


def downgrade() -> None:
    op.drop_index("ix_categories_parent_id")
    op.drop_column("categories", "is_system")
    op.drop_column("categories", "slug")
    op.drop_column("categories", "description")
    op.drop_column("categories", "parent_id")
