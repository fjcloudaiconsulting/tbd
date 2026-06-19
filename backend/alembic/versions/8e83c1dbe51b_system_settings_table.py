"""system_settings table

Revision ID: 8e83c1dbe51b
Revises: 065_import_source_format_add_tab
Create Date: 2026-06-19

Creates ``system_settings`` as a global key/value store for system-wide
feature-flag defaults and configuration.  Mirrors ``org_settings`` but
without an ``org_id`` — each ``key`` is unique across the whole platform.

Shape:
- ``id``         — surrogate PK (autoincrement).
- ``key``        — String(100) NOT NULL; unique (``uq_system_settings_key``).
- ``value``      — Text NOT NULL; JSON-serialised string for flag values.
- ``updated_at`` — DateTime server_default + onupdate now(); the service
                   never needs to manage this column explicitly.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "8e83c1dbe51b"
down_revision = "065_import_source_format_add_tab"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_settings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        # onupdate is handled at the ORM layer (parity with org_settings).
        # There is intentionally NO MySQL `ON UPDATE CURRENT_TIMESTAMP` here,
        # so raw-SQL writes won't auto-bump this column — only ORM writes do.
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        "uq_system_settings_key",
        "system_settings",
        ["key"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_system_settings_key",
        "system_settings",
        type_="unique",
    )
    op.drop_table("system_settings")
