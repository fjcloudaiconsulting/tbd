"""add password_set and step-up token columns to users (L1.7).

Adds three columns:

- `password_set` (bool, default True) — flips False on Google SSO user
  creation so the change-password endpoint can branch on first set.
  Existing rows default to True (they all signed up via password).
- `stepup_token` (varchar 128, nullable) — single-use, short-lived
  token issued by the SSO step-up callback and consumed by the email
  change endpoint as an alternative to `current_password`.
- `stepup_token_expires_at` (datetime, nullable) — hard 5-minute
  expiry checked when the email-change handler validates the token.

Revision ID: 031_password_set_stepup
Revises: 030_audit_events
"""
from alembic import op
import sqlalchemy as sa

revision = "031_password_set_stepup"
down_revision = "030_audit_events"


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("password_set", sa.Boolean(), nullable=False, server_default="1"),
    )
    op.add_column(
        "users",
        sa.Column("stepup_token", sa.String(128), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("stepup_token_expires_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "stepup_token_expires_at")
    op.drop_column("users", "stepup_token")
    op.drop_column("users", "password_set")
