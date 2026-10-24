"""Emailed password reset timestamp: users.password_reset_at (BE-03).

Revision ID: 093_password_reset_at
Revises: 092_mfa_email_fallback
Create Date: 2026-10-24

One additive column on ``users``:

    password_reset_at  DATETIME NULL   -- last reset through the emailed link

For 72 hours after it the email-code MFA fallback is refused, even for an
account that opted in, so the next sign-in after a reset takes TOTP or a
recovery code. ``password_changed_at`` cannot serve: an in-app password change
moves it too, and that change does not go through the mailbox.

Live-safe: a nullable column with no default is ``ALGORITHM=INSTANT`` on
MySQL 8 (metadata only, no table copy). No backfill: NULL means "no reset
recorded", so no existing account is locked out of anything; resets from
before this revision simply open no window.
"""
import sqlalchemy as sa
from alembic import op

revision = "093_password_reset_at"
down_revision = "092_mfa_email_fallback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("password_reset_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "password_reset_at")
