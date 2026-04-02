"""Add recurring_transactions table and billing_cycle_day to organizations

Revision ID: 011
Revises: 010
Create Date: 2026-04-02

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "011"
down_revision: Union[str, None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("billing_cycle_day", sa.Integer(), nullable=False, server_default="1"),
    )

    op.create_table(
        "recurring_transactions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(255), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column(
            "type",
            sa.Enum("income", "expense", name="recurringtxtype"),
            nullable=False,
        ),
        sa.Column(
            "frequency",
            sa.Enum("weekly", "biweekly", "monthly", "quarterly", "yearly", name="frequency"),
            nullable=False,
        ),
        sa.Column("next_due_date", sa.Date(), nullable=False),
        sa.Column("auto_settle", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"]),
    )
    op.create_index("ix_recurring_org", "recurring_transactions", ["org_id"])
    op.create_index("ix_recurring_next_due", "recurring_transactions", ["next_due_date"])


def downgrade() -> None:
    op.drop_index("ix_recurring_next_due")
    op.drop_index("ix_recurring_org")
    op.drop_table("recurring_transactions")
    op.drop_column("organizations", "billing_cycle_day")

    # Drop enum types on PostgreSQL
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        sa.Enum(name="recurringtxtype").drop(bind, checkfirst=True)
        sa.Enum(name="frequency").drop(bind, checkfirst=True)
