"""Recurring transfers: a destination account and a transfer type (TBD-600).

Revision ID: 098_recurring_transfers
Revises: 097_merchant_dictionary_votes
Create Date: 2026-11-03

Two changes on ``recurring_transactions``:

    to_account_id  INT NULL  FK -> accounts(id)
    type           ENUM('income','expense') -> ENUM('income','expense','transfer')

A ``transfer`` series generates a linked transfer pair (an expense leg on
``account_id``, an income leg on ``to_account_id``) instead of one row, so a
plan can fund a savings account without the money reading as spending.

Live-safe: the nullable column is ``ALGORITHM=INSTANT`` on MySQL 8, and
appending a value to the END of an ENUM is a metadata-only change. The FK is
built with a table copy, which is brief here: one row per template. No
backfill: every existing template keeps NULL, which is what income and
expense mean. ``downgrade`` fails on the ENUM narrowing (strict SQL mode) while
any transfer template exists, so it cannot silently rewrite one; delete those
templates first.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "098_recurring_transfers"
down_revision: Union[str, None] = "097_merchant_dictionary_votes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = sa.Enum("income", "expense", name="recurringtxtype")
_NEW = sa.Enum("income", "expense", "transfer", name="recurringtxtype")


def upgrade() -> None:
    op.add_column(
        "recurring_transactions",
        sa.Column("to_account_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_recurring_transactions_to_account_id",
        "recurring_transactions",
        "accounts",
        ["to_account_id"],
        ["id"],
    )
    op.alter_column(
        "recurring_transactions", "type",
        existing_type=_OLD, type_=_NEW, existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "recurring_transactions", "type",
        existing_type=_NEW, type_=_OLD, existing_nullable=False,
    )
    op.drop_constraint(
        "fk_recurring_transactions_to_account_id",
        "recurring_transactions",
        type_="foreignkey",
    )
    op.drop_column("recurring_transactions", "to_account_id")
