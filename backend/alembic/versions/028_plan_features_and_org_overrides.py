"""Add plans.features JSON + org_feature_overrides table.

Revision ID: 028_plan_features
Revises: 027
Create Date: 2026-05-02

L4.11 — adds:
  * plans.features JSON NOT NULL DEFAULT (JSON_OBJECT())
    Backfilled from legacy ai_budget_enabled / ai_forecast_enabled /
    ai_smart_plan_enabled columns. ai.autocategorize defaults False.
    Legacy columns survive for one release as rollback ballast (CLEANUP-029).

  * org_feature_overrides table — single-current per-org boolean overrides
    keyed on UNIQUE(org_id, feature_key).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.sql import column, table

revision = "028_plan_features"
down_revision = "027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add plans.features JSON column with default {}.
    op.add_column(
        "plans",
        sa.Column(
            "features",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("(JSON_OBJECT())"),
        ),
    )

    # 2. Backfill features from legacy ai_* columns. Pass the dict directly;
    #    sa.JSON serializes it as a JSON object. Do NOT json.dumps() — that
    #    stores a quoted string scalar.
    plans_t = table(
        "plans",
        column("id", sa.Integer),
        column("ai_budget_enabled", sa.Boolean),
        column("ai_forecast_enabled", sa.Boolean),
        column("ai_smart_plan_enabled", sa.Boolean),
        column("features", sa.JSON),
    )
    conn = op.get_bind()
    rows = conn.execute(
        sa.select(
            plans_t.c.id,
            plans_t.c.ai_budget_enabled,
            plans_t.c.ai_forecast_enabled,
            plans_t.c.ai_smart_plan_enabled,
        )
    ).fetchall()
    for row in rows:
        features = {
            "ai.budget":         bool(row.ai_budget_enabled),
            "ai.forecast":       bool(row.ai_forecast_enabled),
            "ai.smart_plan":     bool(row.ai_smart_plan_enabled),
            "ai.autocategorize": False,
        }
        conn.execute(
            plans_t.update()
            .where(plans_t.c.id == row.id)
            .values(features=features)
        )

    # 3. Create org_feature_overrides table.
    op.create_table(
        "org_feature_overrides",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "org_id",
            sa.Integer,
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("feature_key", sa.String(64), nullable=False),
        sa.Column("value", sa.Boolean, nullable=False),
        sa.Column(
            "set_by",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("set_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime, nullable=True),
        sa.Column("note", sa.Text, nullable=True),
        sa.UniqueConstraint("org_id", "feature_key", name="uq_org_feature"),
    )
    op.create_index("ix_ofo_expires_at", "org_feature_overrides", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_ofo_expires_at", table_name="org_feature_overrides")
    op.drop_table("org_feature_overrides")
    op.drop_column("plans", "features")
