"""SETTLED-implies-settled_date invariant.

Revision ID: 036_settled_implies_settled_date
Revises: 035_manual_balance_adjustment
Create Date: 2026-05-08

Enforces the business invariant that any ``transactions`` row with
``status='settled'`` must have ``settled_date IS NOT NULL``.

Two changes, in order:

1. Backfill any orphan rows. ``settled_date`` was added in migration 020
   as nullable to keep that change additive. Since then, every code path
   that flips a row to SETTLED has been updated to set ``settled_date``
   alongside, but pre-020 rows or any path that slipped through could
   leave a SETTLED row with a NULL ``settled_date``. The fix:

       UPDATE transactions
          SET settled_date = COALESCE(settled_date, date)
        WHERE status = 'settled' AND settled_date IS NULL;

   We use the row's purchase ``date`` (column name ``date`` on the
   transactions table — see ``effective_period_date_expr()``) as the
   proxy. ``date`` is the only signal we have when ``settled_date`` is
   missing, and ``effective_period_date_expr()`` already falls back to
   it for period bucketing. ``created_at`` is technically available too,
   but it reflects ingestion time, not the financial event, and would
   skew period reports.

2. Add a CHECK constraint:

       CHECK (status <> 'settled' OR settled_date IS NOT NULL)

   MySQL 8.0 enforces CHECK constraints, and SQLite has supported them
   since forever, so this is portable. The constraint is logically
   equivalent to "status = 'settled' implies settled_date IS NOT NULL".

The application layer also has a SQLAlchemy ``@validates`` guard on the
Transaction model that raises ``ValidationError`` before the row hits
the DB, so the CHECK is the second line of defense (data layer truth)
behind the model validator (clear error messages).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "036_settled_implies_settled_date"
down_revision = "035_manual_balance_adjustment"
branch_labels = None
depends_on = None


_CHECK_NAME = "ck_transactions_settled_implies_settled_date"
_CHECK_SQL = "status <> 'settled' OR settled_date IS NOT NULL"


def upgrade() -> None:
    bind = op.get_bind()

    # Step 1: backfill orphan rows so the CHECK is satisfiable.
    # COALESCE keeps any non-NULL settled_date untouched and only fills
    # in the fallback for the (status='settled', settled_date IS NULL)
    # rows the new constraint would otherwise reject.
    bind.execute(
        sa.text(
            "UPDATE transactions "
            "SET settled_date = COALESCE(settled_date, date) "
            "WHERE status = 'settled' AND settled_date IS NULL"
        )
    )

    # Step 2: add the CHECK constraint via batch_alter_table. SQLite
    # has no native ALTER TABLE ADD CONSTRAINT, so Alembic's batch mode
    # rewrites the table to apply CHECK; MySQL 8.0 accepts the batch
    # form via plain ALTER. Plain op.create_check_constraint raises
    # NotImplementedError on SQLite.
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.create_check_constraint(
            _CHECK_NAME,
            _CHECK_SQL,
        )


def downgrade() -> None:
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_constraint(_CHECK_NAME, type_="check")
