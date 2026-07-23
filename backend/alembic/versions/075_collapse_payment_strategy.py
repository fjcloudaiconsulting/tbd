"""Collapse payment_strategy enum to {full_balance, fixed_amount} (F2).

Revision ID: 075_collapse_payment_strategy
Revises: 074_cc_cycle_payments
Create Date: 2026-07-22

F2 reframes per-cycle payment from a STANDING strategy config into a
universal SINGLE-CYCLE override (cc_cycle_payments, honored for any CC by
cc_forecast_service.cc_target_payment). The two members that mismodeled a
per-month decision are dropped:
  keep: full_balance (default, NULL-at-rest), fixed_amount
  drop: minimum_only, custom_per_period
Rows on a dropped strategy reset to NULL (= full_balance default); their
amounts survive as plain overrides in cc_cycle_payments (lossless in intent;
pre-launch, no backcompat). Ordering is load-bearing: the NULL-reset UPDATE
MUST run BEFORE the MODIFY, or MySQL truncates/errors on out-of-set rows.
Raw value tuples (no app-model import), mirroring 045/057/073. VERIFY on a
real MySQL 8 container (upgrade + downgrade + re-upgrade) — SQLite CI cannot
exercise ALTER ... MODIFY ENUM.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "075_collapse_payment_strategy"
down_revision: Union[str, None] = "074_cc_cycle_payments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ENUM_NEW = "ENUM('full_balance','fixed_amount')"
_ENUM_OLD = "ENUM('full_balance','minimum_only','fixed_amount','custom_per_period')"


def upgrade() -> None:
    bind = op.get_bind()
    op.execute(
        text(
            "UPDATE accounts SET payment_strategy = NULL "
            "WHERE payment_strategy IN ('minimum_only', 'custom_per_period')"
        )
    )
    if bind.dialect.name == "mysql":
        op.execute(
            text(f"ALTER TABLE accounts MODIFY COLUMN payment_strategy {_ENUM_NEW} NULL")
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute(
            text(f"ALTER TABLE accounts MODIFY COLUMN payment_strategy {_ENUM_OLD} NULL")
        )
