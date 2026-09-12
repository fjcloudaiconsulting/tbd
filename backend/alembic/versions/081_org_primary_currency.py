"""organizations.primary_currency + backfill (TBD-325 PR 2).

Revision ID: 081_org_primary_currency
Revises: 080_pending_email
Create Date: 2026-09-12

A CACHE of ``accounts.currency``, never a second source of truth.

NULL, never ``server_default='EUR'``: a default manufactures a divergence
for every org whose accounts are not EUR, on migration day.

Backfill: ``SELECT DISTINCT currency`` per org. Exactly-one distinct value ->
that value. Zero accounts or two-or-more distinct values -> NULL, which makes
the scoping predicate return ``true()`` and behaviour byte-identical to today.
"""
import sqlalchemy as sa
from alembic import op

revision = "081_org_primary_currency"
down_revision = "080_pending_email"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("primary_currency", sa.String(length=3), nullable=True),
    )
    # One statement, engine-portable: only orgs with exactly one distinct
    # account currency get a value. GROUP BY ... HAVING COUNT(DISTINCT) = 1.
    #
    # ⚠ ``AS o``, not a bare ``o``. MySQL accepts both; SQLite accepts only the
    # ``AS`` form and dies with ``near "o": syntax error`` on the other, so the
    # bare alias makes ``alembic upgrade head`` unrunnable on SQLite -- which
    # tests/migrations/test_sqlite_portability.py's docstring says migrations
    # must remain, and which is what lets the backfill be fenced at all (CI's
    # shards are aiosqlite; the MySQL leg runs only in Migration Checks).
    op.execute(
        """
        UPDATE organizations AS o
        SET primary_currency = (
            SELECT UPPER(TRIM(MIN(a.currency))) FROM accounts a
            WHERE a.org_id = o.id
            GROUP BY a.org_id
            HAVING COUNT(DISTINCT UPPER(TRIM(a.currency))) = 1
        )
        """
    )


def downgrade() -> None:
    op.drop_column("organizations", "primary_currency")
