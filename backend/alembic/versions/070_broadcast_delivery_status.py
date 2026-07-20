"""Add delivery_status/delivery_updated_at columns + email index to
email_broadcast_recipients.

Revision ID: 070_broadcast_delivery_status
Revises: 069_email_broadcast
Create Date: 2026-07-20

Mailgun delivery webhooks (spec 2026-07-20). Adds two NEW NULLABLE columns:

- ``delivery_status`` ``VARCHAR(32)`` NULL — app-validated, NOT a DB enum
  (Ruling W8, same discipline as ``segment`` — avoids the MySQL
  ALTER-ENUM landmine on an axis that may grow). Values: ``delivered`` /
  ``bounced_permanent`` / ``bounced_temporary`` / ``complained``.
- ``delivery_updated_at`` ``DATETIME`` NULL.

Plus ``INDEX(broadcast_id, email)`` to serve the webhook's correlation
lookup (Ruling W7): ``WHERE broadcast_id=? AND lower(email)=lower(?)``.

All-nullable additive change — no backfill needed. Verified up/down on real
MySQL (isolated ``-p team-*`` stack) per Ruling W8 — nullable ADD COLUMN is
``ALGORITHM=INSTANT`` on MySQL 8, but SQLite CI green alone does not prove
that (the ABN `.TAB` native-ENUM landmine class).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "070_broadcast_delivery_status"
down_revision = "069_email_broadcast"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "email_broadcast_recipients",
        sa.Column("delivery_status", sa.String(32), nullable=True),
    )
    op.add_column(
        "email_broadcast_recipients",
        sa.Column("delivery_updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_broadcast_recipient_email",
        "email_broadcast_recipients",
        ["broadcast_id", "email"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_broadcast_recipient_email", table_name="email_broadcast_recipients"
    )
    op.drop_column("email_broadcast_recipients", "delivery_updated_at")
    op.drop_column("email_broadcast_recipients", "delivery_status")
