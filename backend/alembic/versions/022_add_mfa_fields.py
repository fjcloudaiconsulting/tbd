"""Add MFA fields to users table

Revision ID: 022
Revises: 021
"""

from alembic import op
import sqlalchemy as sa

revision = "022"
down_revision = "021"


def upgrade() -> None:
    op.add_column("users", sa.Column("mfa_enabled", sa.Boolean(), nullable=False, server_default="0"))
    op.add_column("users", sa.Column("totp_secret", sa.String(256), nullable=True))
    op.add_column("users", sa.Column("recovery_codes", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "recovery_codes")
    op.drop_column("users", "totp_secret")
    op.drop_column("users", "mfa_enabled")
