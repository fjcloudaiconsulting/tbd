"""Recurring templates: the latest occurrence spent (TBD-509).

Revision ID: 102_recurring_last_spent_date
Revises: 099_recurring_transfers
Create Date: 2026-11-04

One change on ``recurring_transactions``:

    last_spent_date  DATE NULL

``_advance_frontier`` writes it as it consumes an occurrence, and
``update_recurring`` refuses a next due date on or before it, so moving the
frontier back never re-covers a spent occurrence, even one whose row was
deleted or unlinked by a stop.

Live-safe: a nullable column is ``ALGORITHM=INSTANT`` on MySQL 8. No backfill:
a template that spent occurrences before this column existed keeps NULL, and
the guard then refuses any move earlier than its current frontier. Only a
generation run that spends an occurrence sets the column and lifts that, so a
paused or exhausted legacy template keeps the restriction.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "102_recurring_last_spent_date"
down_revision: Union[str, None] = "099_recurring_transfers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "recurring_transactions",
        sa.Column("last_spent_date", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("recurring_transactions", "last_spent_date")
