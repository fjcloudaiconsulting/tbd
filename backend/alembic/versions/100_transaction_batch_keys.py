"""Idempotency keys for the manual batch create (TBD-784).

Revision ID: 100_transaction_batch_keys
Revises: 099_recurring_transfers
Create Date: 2026-11-04

One new table, ``transaction_batch_keys``: one row per keyed
``POST /api/v1/transactions/batch``, holding the request hash and the stored
response, ``UNIQUE (idempotency_key, org_id, user_id)``. The import-confirm key
(095) lives on the ``import_batches`` header that confirm creates; a manual
batch has no header row, so it gets its own table.

Live-safe: a new, empty table; nothing existing is altered or locked. The key
leads the unique index so MySQL keeps separate indexes for the two FKs (see
095).
"""
import sqlalchemy as sa
from alembic import op

revision = "100_transaction_batch_keys"
down_revision = "099_recurring_transfers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "transaction_batch_keys",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "org_id", sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "user_id", sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "idempotency_key", "org_id", "user_id",
            name="uq_transaction_batch_keys_key",
        ),
    )
    op.create_index(
        "ix_transaction_batch_keys_created_at",
        "transaction_batch_keys",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_table("transaction_batch_keys")
