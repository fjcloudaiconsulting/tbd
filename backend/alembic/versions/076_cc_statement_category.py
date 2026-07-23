"""cc statement notification category + prefs

Revision ID: 076_cc_statement_category
Revises: 075_collapse_payment_strategy
Create Date: 2026-07-23

CC Statement Alerts V1 Task 1 — adds a fifth notification category
(``cc_statement``) that the reminder + close scheduler jobs (later
tasks in this feature) will dispatch through, plus its per-user
preference columns (``email_cc_statement`` / ``in_app_cc_statement``,
default ON/opt-out — mirrors ``email_account``/``in_app_account``, NOT
the opt-in ``org_activity`` shape).

The ``notifications.category`` column is a native MySQL ENUM (see
migration 045/057/073's precedent), so the widening ``MODIFY COLUMN``
is MySQL-guarded — SQLite (used by the unit-test suite) has no native
ENUM and does not need the DDL. The downgrade must remap any
``cc_statement`` rows to ``org_activity`` BEFORE narrowing the ENUM
back to four values, or MySQL truncates/errors on the out-of-set rows
(same ordering hazard 075 documents for ``payment_strategy``). VERIFY
on a real MySQL 8 container (upgrade + downgrade + re-upgrade) —
SQLite CI cannot exercise ``ALTER ... MODIFY ENUM``.

Revision id is deliberately short (25 chars, not the more verbose
``076_cc_statement_notification_category``): ``alembic_version.version_num``
is ``VARCHAR(32)`` and a longer id truncates the stamp write with a
1406 ``Data too long`` error AFTER the DDL has already applied
non-transactionally, leaving the DB schema ahead of what
``alembic current`` reports. Hit this exact failure mode locally
before landing on this id.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "076_cc_statement_category"
down_revision: Union[str, None] = "075_collapse_payment_strategy"
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None


_OLD = "ENUM('security','account','org_admin','org_activity') NOT NULL"
_NEW = "ENUM('security','account','org_admin','org_activity','cc_statement') NOT NULL"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute(f"ALTER TABLE notifications MODIFY COLUMN category {_NEW}")
    op.add_column(
        "user_notification_preferences",
        sa.Column("email_cc_statement", sa.Boolean(), nullable=False, server_default="1"),
    )
    op.add_column(
        "user_notification_preferences",
        sa.Column("in_app_cc_statement", sa.Boolean(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_column("user_notification_preferences", "in_app_cc_statement")
    op.drop_column("user_notification_preferences", "email_cc_statement")
    if bind.dialect.name == "mysql":
        # Remap any rows using the new value before narrowing the ENUM,
        # else the MODIFY fails / truncates out-of-set rows.
        op.execute("UPDATE notifications SET category='org_activity' WHERE category='cc_statement'")
        op.execute(f"ALTER TABLE notifications MODIFY COLUMN category {_OLD}")
