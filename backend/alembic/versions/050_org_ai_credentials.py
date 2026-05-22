"""Per-org AI provider credentials (PR1 of AI tier train).

Revision ID: 050_org_ai_credentials
Revises: 049_announcements
Create Date: 2026-05-22

Creates the single ``org_ai_credentials`` table that backs the BYO
provider-key substrate. The composite ``UNIQUE (id, org_id)`` named
constraint exists so later migrations can reference ``(org_id, id)``
as a composite FK target -- routing, caps, and consents all use it
for DB-level cross-org refusal.

Scope expansion since the original PR1 spec: routing / caps / consents
and the native adapter shell now ship in the same PR, via migrations
051 / 052 / 053. The usage ledger and the real native backend remain
out of scope and land in subsequent PRs. See
``specs/2026-05-22-ai-tier-byo-and-native-providers.md``.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "050_org_ai_credentials"
down_revision = "049_announcements"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "org_ai_credentials",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "org_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "provider",
            sa.Enum(
                "openai",
                "anthropic",
                "ollama",
                "openai_compatible",
                name="ai_provider",
                values_callable=lambda x: list(x),
            ),
            nullable=False,
        ),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("encrypted_bearer_token", sa.Text(), nullable=True),
        sa.Column("base_url", sa.String(length=512), nullable=True),
        sa.Column("key_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("last_four", sa.String(length=8), nullable=False),
        sa.Column("discovered_capabilities", sa.JSON(), nullable=True),
        sa.Column("discovered_models", sa.JSON(), nullable=True),
        sa.Column("label", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("last_validated_at", sa.DateTime(), nullable=True),
        sa.Column("validation_error", sa.String(length=500), nullable=True),
        # Named composite UNIQUE so future PRs (routing / caps) can
        # reference (org_id, id) as a composite FK target and rely on
        # this constraint name for the parent index.
        sa.UniqueConstraint(
            "org_id", "id", name="uq_org_ai_credentials_org_id_id"
        ),
    )
    op.create_index(
        "ix_org_ai_credentials_org_id",
        "org_ai_credentials",
        ["org_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_org_ai_credentials_org_id", table_name="org_ai_credentials"
    )
    op.drop_table("org_ai_credentials")
    # Drop the enum type only on backends that materialize enums (e.g.
    # PostgreSQL). MySQL stores the enum inline with the column, so the
    # type is gone with the table.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        sa.Enum(name="ai_provider").drop(bind, checkfirst=True)
