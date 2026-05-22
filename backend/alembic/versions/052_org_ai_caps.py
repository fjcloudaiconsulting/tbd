"""Per-org AI spend caps (PR1 follow-up).

Revision ID: 052_org_ai_caps
Revises: 051_org_ai_routing
Create Date: 2026-05-22

Split caps tables, same nullable-unique reason as routing (spec §7).
Enforcement / ledger writes ride in PR2; PR1 only ships the tables +
admin CRUD so caps can be configured ahead of the dispatch wiring.

Spec stores ``soft_cap_cents`` / ``hard_cap_cents`` as nullable INT
(cents) — that's what the PR2 ``call_llm`` chokepoint reads. We honor
that rather than DECIMAL(10,2) USD because INT cents is the existing
convention everywhere else in the spec and avoids float-equality
surprises in the cap-check path.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "052_org_ai_caps"
down_revision = "051_org_ai_routing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "org_ai_default_caps",
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("soft_cap_cents", sa.Integer(), nullable=True),
        sa.Column("hard_cap_cents", sa.Integer(), nullable=True),
        sa.Column(
            "period",
            sa.String(length=20),
            nullable=False,
            server_default="monthly",
        ),
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
        sa.PrimaryKeyConstraint("org_id", name="pk_org_ai_default_caps"),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_default_caps_org",
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "org_ai_feature_caps",
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("feature_key", sa.String(length=120), nullable=False),
        sa.Column("soft_cap_cents", sa.Integer(), nullable=True),
        sa.Column("hard_cap_cents", sa.Integer(), nullable=True),
        sa.Column(
            "period",
            sa.String(length=20),
            nullable=False,
            server_default="monthly",
        ),
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
            "org_id", "feature_key", name="pk_org_ai_feature_caps"
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_feature_caps_org",
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    op.drop_table("org_ai_feature_caps")
    op.drop_table("org_ai_default_caps")
