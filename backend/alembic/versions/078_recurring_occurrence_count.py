"""Instalment series: occurrence_count + occurrences_elapsed (TBD-275).

Revision ID: 078_recurring_occurrence_count
Revises: 077_loan_account_type
Create Date: 2026-08-03

Two additive columns on ``recurring_transactions``:

    occurrence_count     Integer NULL           -- declared total, NULL = open-ended
    occurrences_elapsed  Integer NOT NULL DEF 0 -- stored progress

Both plain integers, so there is no native-ENUM or collation landmine and
SQLite CI mirrors MySQL prod faithfully (``reference_abn_tab_import``).

**No backfill, deliberately.** Every pre-existing template IS open-ended --
that is exactly what ``occurrence_count IS NULL`` means -- so there is nothing
to recover. Backfilling ``occurrences_elapsed`` from
``COUNT(*) FROM transactions WHERE recurring_id = ...`` would be actively
wrong: ``stop_recurring`` NULLs ``recurring_id`` on every surviving row, users
delete materialised rows, and the catch-up loop's ``exists`` branch advances
the frontier without writing a row. The count is therefore an UNDER-count of
what the series has delivered, and seeding a counter from it would hand every
legacy template phantom budget. 0 is the correct seed for a NULL count because
a NULL count never consults the counter at all.

**No index.** ``recurring_filters.active_series_filter()`` adds
``occurrence_count IS NULL OR occurrences_elapsed < occurrence_count`` to
queries that are already bounded by ``org_id`` (and usually by
``next_due_date``). Both are low-cardinality residual predicates evaluated
after that access path; an index on either would never be chosen.
``server_default="0"`` is what makes the NOT NULL column addable on a
non-empty table in one statement.
"""
import sqlalchemy as sa
from alembic import op

revision = "078_recurring_occurrence_count"
down_revision = "077_loan_account_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "recurring_transactions",
        sa.Column("occurrence_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "recurring_transactions",
        sa.Column(
            "occurrences_elapsed",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("recurring_transactions", "occurrences_elapsed")
    op.drop_column("recurring_transactions", "occurrence_count")
