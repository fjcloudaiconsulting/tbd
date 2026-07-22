"""Add Credit Card Model V1 fields to accounts (Slice 1).

Revision ID: 073_credit_card_model_v1
Revises: 072_payment_source_account_id
Create Date: 2026-07-22

Four additive nullable columns on ``accounts`` (all NULL on non-CC rows,
mirroring the close_day fat-row invariant; no server_default):

    credit_limit          Numeric(12,2) NULL  -- optional, > 0 if set
    apr                   Numeric(12,2) NULL  -- percent metadata [0,100]
    fixed_payment_amount  Numeric(12,2) NULL  -- required iff strategy=fixed_amount
    payment_strategy      ENUM(...)     NULL  -- native MySQL enum, closed 4-set

``payment_strategy`` is a native MySQL ENUM. The set is genuinely CLOSED
(4 members), so the ABN .TAB enum-growth rule does not apply. Raw value
tuples are passed to ``sa.Enum`` (NOT the Python enum) so this migration
never imports app models, matching 045_reconciliation_state.py.

VERIFY with ``alembic upgrade head`` against a MySQL 8 container before
merge — SQLite CI cannot catch native-ENUM DDL drift.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "073_credit_card_model_v1"
down_revision: Union[str, None] = "072_payment_source_account_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Lower-case values, matching the project's
# ``values_callable=lambda x: [e.value for e in x]`` convention.
_STRATEGIES = (
    "full_balance",
    "minimum_only",
    "fixed_amount",
    "custom_per_period",
)


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("credit_limit", sa.Numeric(12, 2), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column("apr", sa.Numeric(12, 2), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column("fixed_payment_amount", sa.Numeric(12, 2), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column(
            "payment_strategy",
            sa.Enum(*_STRATEGIES, name="account_payment_strategy"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    # Named enums on MySQL are stored inline on the column, so dropping
    # the column drops the enum. No separate Enum.drop() needed.
    op.drop_column("accounts", "payment_strategy")
    op.drop_column("accounts", "fixed_payment_amount")
    op.drop_column("accounts", "apr")
    op.drop_column("accounts", "credit_limit")
