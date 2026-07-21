"""Create api_tokens table.

Revision ID: 071_api_tokens
Revises: 070_broadcast_delivery_status
Create Date: 2026-07-21

Superadmin personal access tokens (PAT, spec 2026-07-2x). One row per
issued token:

- ``token_hash`` ``VARCHAR(64)`` — SHA-256 hex digest of the HMAC-peppered
  token (see Task 1's ``app.security_pat`` helpers). UNIQUE + indexed:
  the auth path looks a presented token up by its hash.
- ``token_prefix`` ``VARCHAR(16)`` — short non-secret slice for admin-UI
  display only.
- ``scope`` ``VARCHAR(16)`` — app-validated ``read`` | ``write``, NOT a
  native MySQL ENUM (same discipline as ``email_broadcasts.segment`` in
  migration 069 — dodges the ALTER-ENUM landmine on a growth axis).
- ``created_by_user_id`` FK to ``users.id`` ``ON DELETE SET NULL`` +
  ``created_by_email`` snapshot, matching the ``audit_events`` /
  ``email_broadcast_recipients`` convention: the record of who minted a
  token survives that user's later deletion.

Verified up/down on real MySQL (isolated ``-p team-pat`` stack) — SQLite CI
green does not prove MySQL DDL (index length / FK-cover class of bug).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "071_api_tokens"
down_revision = "070_broadcast_delivery_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("token_prefix", sa.String(16), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_by_email", sa.String(255), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_ip", sa.String(45), nullable=True),
        sa.Column(
            "reminder_stage",
            sa.SmallInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_api_tokens_created_by",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_api_tokens_token_hash", "api_tokens", ["token_hash"], unique=True
    )
    op.create_index(
        "ix_api_tokens_created_by_user_id", "api_tokens", ["created_by_user_id"]
    )


def downgrade() -> None:
    # No explicit op.drop_index calls here. MySQL InnoDB rejects dropping
    # an index that still covers an FK constraint with errno 1553 (the
    # ix_api_tokens_created_by_user_id index covers fk_api_tokens_created_by)
    # — dropping the table drops its indexes and FK automatically, and we
    # don't need the indexes once the table itself is gone. Same pattern as
    # migration 038's downgrade. Cross-reference: reference_mysql_fk_index_cover.md.
    op.drop_table("api_tokens")
