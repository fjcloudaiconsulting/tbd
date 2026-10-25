"""Opt-in email-code MFA fallback: users.mfa_email_fallback (BE-03).

Revision ID: 092_mfa_email_fallback
Revises: 091_micro_usd_metering
Create Date: 2026-10-24

One additive column on ``users``:

    mfa_email_fallback  BOOL NOT NULL DEFAULT 0

Whether a code mailed to the account address may stand in for TOTP at sign-in.
The default is the decision: every existing account, TOTP or not, starts with
the fallback off, and the user turns it on in security settings. No account can
hold MFA without a TOTP secret (``mfa_enable`` needs a TOTP code), so nobody
loses their only second factor; the sign-in check also keeps the email code for
such an account should one ever exist.

Live-safe: adding a column with a constant default is ``ALGORITHM=INSTANT`` on
MySQL 8 (metadata only, no table copy, no backfill), and every existing row
reads the default. ``downgrade`` drops the column and with it the opt-ins.
"""
import sqlalchemy as sa
from alembic import op

revision = "092_mfa_email_fallback"
down_revision = "091_micro_usd_metering"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "mfa_email_fallback", sa.Boolean(), nullable=False, server_default="0"
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "mfa_email_fallback")
