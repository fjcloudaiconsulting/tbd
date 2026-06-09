"""Add 'tab' to the import_batches.source_format enum (ABN AMRO .TAB import).

Revision ID: 065_import_source_format_add_tab
Revises: 064_report_versions
Create Date: 2026-06-09

``import_batches.source_format`` is a native MySQL ``ENUM('csv','ofx')``
column (SQLAlchemy ``Enum`` with ``values_callable``). Adding ``TAB`` to
the Python enum alone is NOT enough: a ``.TAB`` confirm would pass CI
(SQLite test DBs are built from the models via ``create_all``, so they
pick up the new value) but 500 on production MySQL when inserting the
``import_batches`` row, because MySQL rejects an out-of-set ENUM value.
The preview endpoint is unaffected (in-memory); only confirm writes the
column.

The existing column is ``ENUM('csv','ofx') NOT NULL`` (no default — see
``045_reconciliation_state``). The MODIFY below preserves NOT NULL and
adds ``'tab'``. MySQL stores enums inline on the column, so no separate
type object to alter. SQLite rebuilds the schema from the models each
test run, so this is a no-op there.

Spec: ``specs/2026-06-09-abn-tab-import.md`` ("Migration").
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text


revision = "065_import_source_format_add_tab"
down_revision = "064_report_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute(
            text(
                "ALTER TABLE import_batches "
                "MODIFY COLUMN source_format "
                "ENUM('csv','ofx','tab') NOT NULL"
            )
        )
    # SQLite stores enums as strings + a CHECK; test fixtures rebuild the
    # schema each run, so no-op here.


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        # Pre-launch: safe to assume no 'tab' rows exist yet.
        op.execute(
            text(
                "ALTER TABLE import_batches "
                "MODIFY COLUMN source_format "
                "ENUM('csv','ofx') NOT NULL"
            )
        )
    # SQLite downgrade is a no-op.
