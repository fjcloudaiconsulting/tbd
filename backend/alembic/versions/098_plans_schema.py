"""Plans schema, additive only: a scenario becomes a plan, plus plan_goals (TBD-598).

Revision ID: 098_plans_schema
Revises: 097_merchant_dictionary_votes
Create Date: 2026-11-03

First ticket of the Plans redesign (TBD-548). Every legacy ``scenarios``
column stays readable; the drops happen in the cleanup ticket.

New ``scenarios`` columns are nullable or carry a server default, so existing
rows are valid with no data change: ``status`` 'draft', ``return_pct`` and
``inflation_pct`` 0, the rest NULL. ``currency`` is NULL, meaning the org's
primary currency; there is no backfill (``organizations.primary_currency`` is
itself nullable, so a copy could not make the column required anyway).

The three FKs on ``scenarios`` are ``ON DELETE SET NULL`` so org reset and
delete, which wipe accounts, categories and tags, never trip them.
``scenarios`` is small (a few plans per user), so the FK table copy is short.

New table ``plan_goals``, deleted with its plan (``scenario_id`` CASCADE) and
with its org. It is not called ``plans``: subscription tiers own that name.
Named enums are stored inline on MySQL columns, so dropping a column or the
table drops its enum.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "098_plans_schema"
down_revision: Union[str, None] = "097_merchant_dictionary_votes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Raw values, not the app enums, so this migration never imports app models.
_STATUSES = ("draft", "active", "finished")
_TIERS = ("essential", "want", "dream")
_KINDS = ("lump", "monthly_spend", "savings_target", "retirement")

# (column, referenced table) for the new SET NULL foreign keys on scenarios.
_SCENARIO_FKS = (
    ("funding_account_id", "accounts"),
    ("category_id", "categories"),
    ("tag_id", "tags"),
)


def upgrade() -> None:
    op.add_column(
        "scenarios",
        sa.Column(
            "status",
            sa.Enum(*_STATUSES, name="scenario_status"),
            nullable=False,
            server_default="draft",
        ),
    )
    op.add_column("scenarios", sa.Column("currency", sa.CHAR(3), nullable=True))
    op.add_column("scenarios", sa.Column("monthly_budget", sa.Numeric(12, 2), nullable=True))
    op.add_column("scenarios", sa.Column("birth_year", sa.SmallInteger(), nullable=True))
    for name in ("return_pct", "inflation_pct"):
        op.add_column(
            "scenarios",
            sa.Column(name, sa.Numeric(5, 2), nullable=False, server_default="0.00"),
        )
    for column, _ in _SCENARIO_FKS:
        op.add_column("scenarios", sa.Column(column, sa.Integer(), nullable=True))
    op.add_column("scenarios", sa.Column("started_at", sa.DateTime(), nullable=True))
    op.add_column("scenarios", sa.Column("finished_at", sa.DateTime(), nullable=True))
    op.add_column("scenarios", sa.Column("baseline_projection_json", sa.JSON(), nullable=True))
    for column, table in _SCENARIO_FKS:
        op.create_foreign_key(
            f"fk_scenarios_{column}", "scenarios", table, [column], ["id"],
            ondelete="SET NULL",
        )

    op.create_table(
        "plan_goals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "org_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "scenario_id",
            sa.Integer(),
            sa.ForeignKey("scenarios.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("tier", sa.Enum(*_TIERS, name="plan_goal_tier"), nullable=False),
        sa.Column("kind", sa.Enum(*_KINDS, name="plan_goal_kind"), nullable=False),
        sa.Column("target_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("target_date", sa.Date(), nullable=True),
        sa.Column("monthly_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "category_id",
            sa.Integer(),
            sa.ForeignKey("categories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("start_balance", sa.Numeric(12, 2), nullable=True),
        sa.Column(
            "funding_recurring_id",
            sa.Integer(),
            sa.ForeignKey("recurring_transactions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("params_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_plan_goals_org_id", "plan_goals", ["org_id"])
    op.create_index(
        "ix_plan_goals_scenario_sort", "plan_goals", ["scenario_id", "sort_order"]
    )


def downgrade() -> None:
    op.drop_table("plan_goals")
    for column, _ in _SCENARIO_FKS:
        op.drop_constraint(f"fk_scenarios_{column}", "scenarios", type_="foreignkey")
    for column in (
        "baseline_projection_json",
        "finished_at",
        "started_at",
        *(c for c, _ in reversed(_SCENARIO_FKS)),
        "inflation_pct",
        "return_pct",
        "birth_year",
        "monthly_budget",
        "currency",
        "status",
    ):
        op.drop_column("scenarios", column)
