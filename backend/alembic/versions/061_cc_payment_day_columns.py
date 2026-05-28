"""Add payment_day and payment_day_relative_month to accounts (Slice 1).

Revision ID: 061_cc_payment_day_columns
Revises: 060_rate_limit_overrides
Create Date: 2026-05-28

Per-CC cycle substrate (spec 2026-05-28-cc-billing-cycle.md, Slice 1 /
D3 / D7).

Two new columns on ``accounts``:

- ``payment_day TINYINT NULL``          — the day-of-month the payment
  is due. NULL means "use resolver default" (day 1). User-settable;
  the resolver clamps 31 → Feb 28/29, Apr 30, etc. at compute time.

- ``payment_day_relative_month TINYINT NULL``  — how many calendar
  months after the close month the payment falls. NULL means "use
  resolver default" (1 = next calendar month after close). 0 = same
  month as close; 1 = one month later, etc.

Both columns are intentionally **nullable with NO DB-level default**.
A server default of 1 / 1 would silently fill those values on insert
and break the "NULL = use resolver default" invariant D3 relies on:
existing rows get NULL and the resolver treats NULL as default at
compute time. The validation layer (account_type_change_service) is the
single write surface; it enforces the CC-only invariant (both columns
must be NULL on non-CC accounts, mirrors close_day).

Shape mirrors ``close_day`` (migration 007, sa.Integer(), nullable=True,
no server_default). Downgrade drops both columns with no data loss
concern (Slice 1 adds columns; no stored data depends on them yet).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "061_cc_payment_day_columns"
down_revision = "060_rate_limit_overrides"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("payment_day", sa.Integer(), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column("payment_day_relative_month", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("accounts", "payment_day_relative_month")
    op.drop_column("accounts", "payment_day")
