"""plans and subscriptions

Revision ID: 023
Revises: 022
Create Date: 2026-04-16
"""

from alembic import op
import sqlalchemy as sa

revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plans",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("slug", sa.String(50), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("is_custom", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("price_monthly", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("price_yearly", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("max_users", sa.Integer(), nullable=True),
        sa.Column("retention_days", sa.Integer(), nullable=True),
        sa.Column("ai_budget_enabled", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("ai_forecast_enabled", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("ai_smart_plan_enabled", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("trialing", "active", "past_due", "canceled", name="subscriptionstatus"),
            nullable=False,
            server_default="trialing",
        ),
        sa.Column(
            "billing_interval",
            sa.Enum("monthly", "yearly", name="billinginterval"),
            nullable=False,
            server_default="monthly",
        ),
        sa.Column("trial_start", sa.Date(), nullable=True),
        sa.Column("trial_end", sa.Date(), nullable=True),
        sa.Column("current_period_start", sa.Date(), nullable=True),
        sa.Column("current_period_end", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"]),
        sa.UniqueConstraint("org_id"),
    )

    # Seed default plans
    plans_table = sa.table(
        "plans",
        sa.column("name", sa.String),
        sa.column("slug", sa.String),
        sa.column("description", sa.Text),
        sa.column("price_monthly", sa.Numeric),
        sa.column("price_yearly", sa.Numeric),
        sa.column("max_users", sa.Integer),
        sa.column("retention_days", sa.Integer),
        sa.column("ai_budget_enabled", sa.Boolean),
        sa.column("ai_forecast_enabled", sa.Boolean),
        sa.column("ai_smart_plan_enabled", sa.Boolean),
        sa.column("sort_order", sa.Integer),
    )
    op.bulk_insert(
        plans_table,
        [
            {
                "name": "Free",
                "slug": "free",
                "description": "Basic personal finance tracking",
                "price_monthly": 0,
                "price_yearly": 0,
                "max_users": 1,
                "retention_days": 180,
                "ai_budget_enabled": True,
                "ai_forecast_enabled": False,
                "ai_smart_plan_enabled": False,
                "sort_order": 0,
            },
            {
                "name": "Pro",
                "slug": "pro",
                "description": "Full-featured finance management for households",
                "price_monthly": 9.99,
                "price_yearly": 95.88,
                "max_users": 5,
                "retention_days": None,
                "ai_budget_enabled": True,
                "ai_forecast_enabled": True,
                "ai_smart_plan_enabled": True,
                "sort_order": 1,
            },
        ],
    )


def downgrade() -> None:
    op.drop_table("subscriptions")
    op.drop_table("plans")
    # Clean up enum types (PostgreSQL requires explicit drop; MySQL ignores)
    sa.Enum(name="subscriptionstatus").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="billinginterval").drop(op.get_bind(), checkfirst=True)
