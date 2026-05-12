"""Pin users.email to a case-insensitive collation explicitly.

Revision ID: 040_users_email_case_insensitive
Revises: 039_analytics_view_perm
Create Date: 2026-05-12

The default MySQL 8 collation is ``utf8mb4_0900_ai_ci`` which is
already case-insensitive, so on a fresh database the unique
constraint on ``users.email`` already blocks duplicates that
differ only in case. This migration pins the column to that
collation explicitly so a database created against an older
MySQL default (``utf8mb4_general_ci``) or upgraded from a
different default still gets the case-insensitive guarantee. It
is a defense-in-depth measure paired with the Python-side
``normalize_email`` helper used at every user-create site.

The change is a no-op for SQLite (the testing engine). The
``with op.batch_alter_table`` form is portable; MySQL receives an
``ALTER TABLE ... MODIFY COLUMN`` under the hood.

=============================================================
Deployment preflight — REQUIRED before applying in production
=============================================================

If the target database was created on a case-sensitive collation
(``utf8mb4_bin``, ``utf8mb4_0900_as_cs``, or similar) it may
already contain rows that differ only in email casing or
whitespace. Switching the column to ``utf8mb4_0900_ai_ci`` rebuilds
the UNIQUE index over the new comparison rules and will FAIL the
``ALTER TABLE`` with a duplicate-key error if such rows exist.

Run this query against the target DB BEFORE deploying:

    SELECT LOWER(TRIM(email)) AS normalized_email, COUNT(*) AS n
    FROM users
    GROUP BY normalized_email
    HAVING COUNT(*) > 1;

Any row in the result is a collision. Resolve every one of them
via ``POST /api/v1/admin/users/merge`` (the recovery endpoint
shipped in the same PR as this migration) BEFORE running the
migration. Otherwise the unique-constraint rebuild aborts and
``alembic upgrade`` exits non-zero.

The migration intentionally does NOT run this check itself — a
runbook entry the operator reads is clearer than a driver-level
abort, and keeps the migration code portable across MySQL /
SQLite without dialect-specific preflight branching.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision = "040_users_email_case_insensitive"
down_revision = "039_analytics_view_perm"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        # SQLite (tests) — comparison is BINARY by default but the
        # tests don't seed mixed-case duplicate rows, so this is a
        # no-op.
        return
    op.execute(
        "ALTER TABLE users "
        "MODIFY COLUMN email VARCHAR(120) "
        "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    # Revert to the column-level default — the column inherits the
    # table-level collation, which on a fresh MySQL 8 install is
    # already utf8mb4_0900_ai_ci.
    op.execute(
        "ALTER TABLE users "
        "MODIFY COLUMN email VARCHAR(120) NOT NULL"
    )
