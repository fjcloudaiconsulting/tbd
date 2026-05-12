"""Add users.onboarded_at + backfill existing users.

Revision ID: 041_users_onboarded_at
Revises: 040_users_email_case_insensitive
Create Date: 2026-05-12

Adds ``users.onboarded_at TIMESTAMP NULL``. Backfills every existing
row with ``onboarded_at = created_at`` so the first-run wizard does
not greet users who already live in the app. New rows default to
NULL — the L3.3 wizard sets it via ``POST
/api/v1/users/me/onboarding/complete`` once the user finishes (or
skips) the flow.

The column is nullable on purpose: NULL means "has not seen
onboarding yet"; a timestamp means "completed at <ts>". A boolean
would lose the audit trail of when the user finished.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision = "041_users_onboarded_at"
down_revision = "040_users_email_case_insensitive"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("onboarded_at", sa.DateTime(), nullable=True),
    )
    # Backfill: every existing user has already used the app, so they
    # do not need the first-run wizard. Mark them onboarded at their
    # original ``created_at`` so the audit story stays coherent.
    op.execute("UPDATE users SET onboarded_at = created_at WHERE onboarded_at IS NULL")


def downgrade() -> None:
    op.drop_column("users", "onboarded_at")
