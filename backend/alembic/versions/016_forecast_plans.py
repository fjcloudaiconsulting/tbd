"""Add forecast_plans and forecast_plan_items tables

Revision ID: 016
Revises: 015
Create Date: 2026-04-04

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "016"
down_revision: Union[str, None] = "015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "forecast_plans",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("billing_period_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("draft", "active", name="planstatus"),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["billing_period_id"], ["billing_periods.id"]),
        sa.UniqueConstraint("org_id", "billing_period_id", name="uq_forecast_plan_org_period"),
    )
    op.create_index("ix_forecast_plans_org_period", "forecast_plans", ["org_id", "billing_period_id"])

    op.create_table(
        "forecast_plan_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column(
            "type",
            sa.Enum("income", "expense", name="forecastitemtype"),
            nullable=False,
        ),
        sa.Column("planned_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column(
            "source",
            sa.Enum("manual", "recurring", "history", name="itemsource"),
            nullable=False,
            server_default="manual",
        ),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["plan_id"], ["forecast_plans.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"]),
        sa.UniqueConstraint("plan_id", "category_id", "type", name="uq_forecast_item_plan_cat_type"),
    )
    op.create_index("ix_forecast_plan_items_plan", "forecast_plan_items", ["plan_id"])


def downgrade() -> None:
    op.drop_index("ix_forecast_plan_items_plan", table_name="forecast_plan_items")
    op.drop_table("forecast_plan_items")
    op.drop_index("ix_forecast_plans_org_period", table_name="forecast_plans")
    op.drop_table("forecast_plans")
