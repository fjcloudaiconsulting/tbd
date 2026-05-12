"""Add opening_balance + opening_balance_date to accounts (L3.2 Wave 2A).

Revision ID: 041_opening_balance
Revises: 040_users_email_case_insensitive
Create Date: 2026-05-12

Implements the column-shape half of the Opening Balance contract
(``specs/2026-05-12-l3-2-import-contracts.md`` §0.4 row C / §4 / §4.4).

Two additive columns on ``accounts``:

1. ``opening_balance NUMERIC(12, 2) NOT NULL DEFAULT 0`` — user-stated
   starting amount. NOT a derived quantity; backfilled to 0 for every
   existing account by the column-level ``server_default``. The Wave 2A
   UI lets a user enter a non-zero opening balance at account-creation
   time or edit it later via ``PATCH /api/v1/accounts/{id}``.

2. ``opening_balance_date DATE NOT NULL DEFAULT (current_date)`` —
   the date the user states their starting balance applies from. UI
   default is today. Picks up ``current_date`` for backfilled rows.

Backfill (CANONICAL, locked 2026-05-12 in §4.4 of the contract):

    Migration sets ``opening_balance = 0`` for ALL existing accounts.
    Users adjust to their actual starting balance via the Opening
    Balance UI (Wave 2A) after migration. No derived formula. The
    0-backfill is drift-free: editing or deleting a historical
    transaction post-migration does NOT shift the stated starting
    amount, because ``opening_balance`` is a user-stated input, not a
    function of the transaction stream.

The backfill is delivered automatically by the ``NOT NULL DEFAULT 0``
DDL — MySQL 8 and SQLite both fill existing rows with 0 at column-add
time. A defensive post-DDL ``UPDATE`` covers the (already paranoid)
edge case where a dialect leaves the new column NULL on some pre-
existing row, and the post-migration assertion is:

    SELECT COUNT(*) FROM accounts WHERE opening_balance != 0

which MUST return 0 immediately after upgrade. See
``backend/tests/migrations/test_041_opening_balance.py`` for the
regression gate.

Forward-compatibility note: the Reconciliation UI team (Wave 2B) will
chain its migration (``transactions.reconciliation_state``,
``transactions.import_batch_id``, new ``import_batches`` table) off
this revision — so they should set ``down_revision = "041_opening_balance"``
when their PR opens.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "041_opening_balance"
down_revision: Union[str, None] = "040_users_email_case_insensitive"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # opening_balance: column-level server_default "0" delivers the
    # CANONICAL backfill to every pre-existing row on both MySQL 8 and
    # SQLite. Numeric(12, 2) matches accounts.balance / transactions.amount.
    op.add_column(
        "accounts",
        sa.Column(
            "opening_balance",
            sa.Numeric(12, 2),
            nullable=False,
            server_default="0",
        ),
    )

    # opening_balance_date: DATE NOT NULL with the contract's
    # "DEFAULT current_date" semantics. MySQL 8 requires the function
    # default to be parenthesised — ``DEFAULT (CURRENT_DATE)`` — whereas
    # SQLite accepts the bare ``DEFAULT CURRENT_DATE`` form. We pick the
    # dialect-correct literal so the column's server-side default keeps
    # working for ANY future INSERT that omits the field (the existing-
    # row backfill below is a separate, explicit step).
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        date_default = sa.text("(CURRENT_DATE)")
    else:
        date_default = sa.text("CURRENT_DATE")
    op.add_column(
        "accounts",
        sa.Column(
            "opening_balance_date",
            sa.Date(),
            nullable=False,
            server_default=date_default,
        ),
    )

    # Defensive post-DDL backfill for ``opening_balance``. The column-
    # level server_default already fills the new column with 0 at DDL
    # time on both dialects we care about, but this UPDATE is the belt-
    # and-braces guarantee called out by §4.4 / the Test Boundary row
    # (Opening Balance team): post-migration, every row's
    # opening_balance MUST be 0. The WHERE clause is a no-op when the
    # DDL already filled the column correctly.
    bind.execute(
        sa.text(
            "UPDATE accounts SET opening_balance = 0 "
            "WHERE opening_balance IS NULL"
        )
    )


def downgrade() -> None:
    # Symmetric drop. Order is immaterial — both columns are independent
    # additions with no FKs, indexes, or constraints introduced here.
    op.drop_column("accounts", "opening_balance_date")
    op.drop_column("accounts", "opening_balance")
