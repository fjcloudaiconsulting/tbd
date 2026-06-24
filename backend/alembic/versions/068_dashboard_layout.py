"""Create dashboard_layouts table.

Revision ID: 068_dashboard_layout
Revises: 067_ix_transactions_org_type
Create Date: 2026-06-24

Persistence substrate for the customizable dashboard (W4 Phase 1).
One layout row per user: ``owner_user_id`` carries a UNIQUE constraint so the
application can use INSERT-or-UPDATE without separate existence checks.

Columns mirror the ``reports`` table conventions:
- ``layout_json`` + ``canvas_filters_json`` are dialect-agnostic JSON.
- ``schema_version`` = 1 gives a non-breaking upgrade path for future
  layout-schema changes.
- ``owner_user_id`` ON DELETE RESTRICT (safety net; service layer owns the
  user-delete semantics).
- ``org_id`` ON DELETE CASCADE (org-delete takes the row automatically).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "068_dashboard_layout"
down_revision = "067_ix_transactions_org_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dashboard_layouts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("layout_json", sa.JSON(), nullable=False),
        sa.Column("canvas_filters_json", sa.JSON(), nullable=False),
        sa.Column(
            "schema_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_dashboard_layouts_org",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_dashboard_layouts_owner",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_user_id", name="uq_dashboard_layouts_owner"),
    )
    op.create_index(
        "ix_dashboard_layouts_org",
        "dashboard_layouts",
        ["org_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_dashboard_layouts_org", table_name="dashboard_layouts")
    op.drop_table("dashboard_layouts")
