"""Per-org AI routing tables (PR1 of AI tier train, follow-up).

Revision ID: 051_org_ai_routing
Revises: 050_org_ai_credentials
Create Date: 2026-05-22

Split routing tables (architect-locked, see spec §4) — a single table
with a nullable feature_name allows MySQL to admit multiple "default"
rows per org because NULLs in a UNIQUE index are distinct. Two tables
make "exactly one default per org" structural via PK(org_id).

Composite FK ``(org_id, credential_id) REFERENCES org_ai_credentials
(org_id, id)`` makes cross-org routing references fail at the DB layer
in addition to the service-layer check (T14 in the spec's threat model).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "051_org_ai_routing"
down_revision = "050_org_ai_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "org_ai_default_routing",
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("credential_id", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
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
        sa.PrimaryKeyConstraint("org_id", name="pk_org_ai_default_routing"),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_default_routing_org",
            ondelete="CASCADE",
        ),
        # Composite FK — DB-level refusal of cross-org references (T14).
        sa.ForeignKeyConstraint(
            ["org_id", "credential_id"],
            ["org_ai_credentials.org_id", "org_ai_credentials.id"],
            name="fk_default_routing_cred",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_default_cred",
        "org_ai_default_routing",
        ["org_id", "credential_id"],
    )

    op.create_table(
        "org_ai_feature_routing",
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("feature_name", sa.String(length=120), nullable=False),
        sa.Column("credential_id", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
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
        sa.PrimaryKeyConstraint(
            "org_id", "feature_name", name="pk_org_ai_feature_routing"
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_feature_routing_org",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "credential_id"],
            ["org_ai_credentials.org_id", "org_ai_credentials.id"],
            name="fk_feature_routing_cred",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_feature_cred",
        "org_ai_feature_routing",
        ["org_id", "credential_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_feature_cred", table_name="org_ai_feature_routing")
    op.drop_table("org_ai_feature_routing")
    op.drop_index("ix_default_cred", table_name="org_ai_default_routing")
    op.drop_table("org_ai_default_routing")
