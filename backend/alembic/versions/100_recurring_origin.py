"""Recurring transfer legs remember their template (TBD-789).

Revision ID: 100_recurring_origin
Revises: 099_recurring_transfers
Create Date: 2026-11-04

    transactions.recurring_origin_id  INT NULL  (no FK)

``recurring_id`` is cleared when a series is stopped and nulled by its FK when
the series is deleted, so a generated transfer leg into a loan would then read
as a manual payment and cancel that period's scheduled payment in the
forecast. The new column keeps the template id for good.

Live-safe: a nullable column with no FK and no index is ``ALGORITHM=INSTANT``
on MySQL 8. The backfill copies ``recurring_id`` onto the linked legs that
still carry one (only recurring transfers write a linked row with a
``recurring_id``), in id batches of ``BATCH`` rows. Legs whose series was
already stopped or deleted cannot be recovered and stay NULL.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.sql import column, table


revision: str = "100_recurring_origin"
down_revision: Union[str, None] = "099_recurring_transfers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BATCH = 500

_tx = table(
    "transactions",
    column("id"), column("recurring_id"), column("linked_transaction_id"),
    column("recurring_origin_id"),
)


def backfill(conn) -> int:
    """Copy ``recurring_id`` onto linked legs still missing an origin; rows written."""
    ids = list(conn.execute(
        sa.select(_tx.c.id).where(
            _tx.c.recurring_id.is_not(None),
            _tx.c.linked_transaction_id.is_not(None),
            _tx.c.recurring_origin_id.is_(None),
        ).order_by(_tx.c.id)
    ).scalars())
    for start in range(0, len(ids), BATCH):
        conn.execute(
            _tx.update()
            .where(_tx.c.id.in_(ids[start:start + BATCH]))
            .values(recurring_origin_id=_tx.c.recurring_id)
        )
    return len(ids)


def upgrade() -> None:
    op.add_column("transactions", sa.Column("recurring_origin_id", sa.Integer(), nullable=True))
    backfill(op.get_bind())


def downgrade() -> None:
    op.drop_column("transactions", "recurring_origin_id")
