"""Drop legacy plans.ai_*_enabled columns (CLEANUP-029).

Revision ID: 032_drop_legacy_plan_ai
Revises: 031_password_set_stepup
Create Date: 2026-05-07

L4.11 follow-up. Migration 028 added plans.features (JSON) and backfilled
it from the three legacy boolean columns; PlanResponse derived the legacy
booleans from features for one release. Now that the JSON column is the
single source of truth, drop the columns and the dual-read shim.

Downgrade re-adds the columns and backfills from features JSON so a
rollback is non-lossy.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.sql import column, table

revision = "032_drop_legacy_plan_ai"
down_revision = "031_password_set_stepup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("plans", "ai_budget_enabled")
    op.drop_column("plans", "ai_forecast_enabled")
    op.drop_column("plans", "ai_smart_plan_enabled")


def downgrade() -> None:
    # Re-add as nullable=False with server_default="0" so existing rows
    # accept the new column; we then backfill from features JSON.
    op.add_column(
        "plans",
        sa.Column(
            "ai_budget_enabled",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "plans",
        sa.Column(
            "ai_forecast_enabled",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "plans",
        sa.Column(
            "ai_smart_plan_enabled",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
    )

    plans_t = table(
        "plans",
        column("id", sa.Integer),
        column("ai_budget_enabled", sa.Boolean),
        column("ai_forecast_enabled", sa.Boolean),
        column("ai_smart_plan_enabled", sa.Boolean),
        column("features", sa.JSON),
    )
    conn = op.get_bind()
    rows = conn.execute(sa.select(plans_t.c.id, plans_t.c.features)).fetchall()
    for row in rows:
        features = row.features or {}
        conn.execute(
            plans_t.update()
            .where(plans_t.c.id == row.id)
            .values(
                ai_budget_enabled=bool(features.get("ai.budget", False)),
                ai_forecast_enabled=bool(features.get("ai.forecast", False)),
                ai_smart_plan_enabled=bool(features.get("ai.smart_plan", False)),
            )
        )
