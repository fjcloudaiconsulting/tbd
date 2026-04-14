"""add password_changed_at to users

Revision ID: 021
Revises: 020
"""
from alembic import op
import sqlalchemy as sa

revision = "021"
down_revision = "020"


def upgrade() -> None:
    op.add_column("users", sa.Column("password_changed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "password_changed_at")
