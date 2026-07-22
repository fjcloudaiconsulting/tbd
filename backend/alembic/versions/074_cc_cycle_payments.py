"""Create cc_cycle_payments table (Credit Card Model V1, Slice 2).

Revision ID: 074_cc_cycle_payments
Revises: 073_credit_card_model_v1
Create Date: 2026-07-22

Per-cycle CC payment amounts. Anchor = the cycle's CLOSE month
(period_anchor_year / period_anchor_month). No org_id column — org
isolation is enforced at the router by loading the parent account
under the caller's org_id. ``account_id`` FK to accounts.id ON DELETE
CASCADE (a payment row is meaningless without its account). ``amount``
NOT NULL, no CHECK.

Verified up/down on a real MySQL 8 container (isolated ``-p team-ccm1``
stack) — SQLite CI green does not prove MySQL DDL (index-length / FK-
cover class of bug).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "074_cc_cycle_payments"
down_revision: Union[str, None] = "073_credit_card_model_v1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cc_cycle_payments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("period_anchor_year", sa.SmallInteger(), nullable=False),
        sa.Column("period_anchor_month", sa.SmallInteger(), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
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
            ["account_id"],
            ["accounts.id"],
            name="fk_cc_cycle_payments_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id",
            "period_anchor_year",
            "period_anchor_month",
            name="uq_cc_cycle_payments_account_period",
        ),
    )


def downgrade() -> None:
    # Dropping the table drops its FK + unique index automatically.
    op.drop_table("cc_cycle_payments")
