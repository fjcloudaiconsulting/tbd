"""Durable bind marker between a bank row and the recurring occurrence it settles (TBD-755).

Revision ID: 105_transaction_occurrence_bind
Revises: 104_plan_goal_varchar
Create Date: 2026-11-15

    transactions.bind_prev_amount       NUMERIC(12,2) NULL  -- on the bank row
    transactions.bind_prev_date         DATE NULL
    transactions.bind_prev_settled_date DATE NULL
    transactions.bind_prev_status       ENUM('settled','pending') NULL
    transactions.bound_bank_tx_id       INT NULL UNIQUE, FK transactions.id ON DELETE SET NULL
                                                        -- on the occurrence

The four ``bind_prev_*`` columns snapshot the occurrence as it was before a
bank row settled it, so an unbind restores it exactly; a bank row is bound
when ``bind_prev_status`` is set. ``bound_bank_tx_id`` is the occurrence's
back-pointer: every occurrence-side guard reads it off a row it already holds
locked, and the UNIQUE index refuses two bank rows binding one occurrence.

No backfill: binds written before this revision carry no snapshot and stay
not unbindable.

Deploy: the nullable columns are ``ALGORITHM=INSTANT``; the FK makes MySQL copy
``transactions``, so this needs a deploy window.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "105_transaction_occurrence_bind"
down_revision: Union[str, None] = "104_plan_goal_varchar"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FK = "fk_transactions_bound_bank_tx_id"
_UQ = "uq_transactions_bound_bank_tx_id"


def upgrade() -> None:
    op.add_column("transactions", sa.Column("bind_prev_amount", sa.Numeric(12, 2), nullable=True))
    op.add_column("transactions", sa.Column("bind_prev_date", sa.Date(), nullable=True))
    op.add_column("transactions", sa.Column("bind_prev_settled_date", sa.Date(), nullable=True))
    op.add_column("transactions", sa.Column(
        "bind_prev_status",
        sa.Enum("settled", "pending", name="transaction_bind_prev_status"),
        nullable=True,
    ))
    op.add_column("transactions", sa.Column("bound_bank_tx_id", sa.Integer(), nullable=True))
    op.create_unique_constraint(_UQ, "transactions", ["bound_bank_tx_id"])
    op.create_foreign_key(
        _FK, "transactions", "transactions", ["bound_bank_tx_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint(_FK, "transactions", type_="foreignkey")
    op.drop_constraint(_UQ, "transactions", type_="unique")
    for name in (
        "bound_bank_tx_id", "bind_prev_status", "bind_prev_settled_date",
        "bind_prev_date", "bind_prev_amount",
    ):
        op.drop_column("transactions", name)
