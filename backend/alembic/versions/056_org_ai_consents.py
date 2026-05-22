"""Per-org AI consents (PR1 follow-up).

Revision ID: 056_org_ai_consents
Revises: 055_org_ai_caps
Create Date: 2026-05-22

Append-only consent table. New consent => new row, never an UPDATE.
The latest row by ``consented_at`` is the current state. ``revoked_at``
NOT NULL means the org withdrew consent (refusal is structural — native
dispatch refuses regardless of the most recent allow_* booleans when
revoked_at is set).

Spec §5. Native dispatch refuses regardless of consent state when
``AI_NATIVE_ENABLED=false`` (the toggle is the outer gate). Consent
writes are accepted ahead of native going live so the consent history
can be defensible from day one.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "056_org_ai_consents"
down_revision = "055_org_ai_caps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "org_ai_consents",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column(
            "allow_training",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "allow_rag",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "allow_telemetry",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "consent_version", sa.String(length=40), nullable=False
        ),
        sa.Column(
            "consented_by_user_id", sa.Integer(), nullable=True
        ),
        sa.Column(
            "consented_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_consent_org",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["consented_by_user_id"],
            ["users.id"],
            name="fk_consent_user",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_org_active",
        "org_ai_consents",
        ["org_id", "revoked_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_org_active", table_name="org_ai_consents")
    op.drop_table("org_ai_consents")
