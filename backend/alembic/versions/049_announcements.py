"""Operator-authored announcement banner substrate.

Revision ID: 049_announcements
Revises: 048_users_delete_perm
Create Date: 2026-05-22

Creates the two tables that back the announcement banner system
(spec ``specs/2026-05-21-announcement-banner-system.md``):

- ``announcements`` — content rows, severity-tagged, schedule
  window, ``created_by_user_id`` ON DELETE SET NULL so a deleted
  superadmin doesn't drop the announcement row.
- ``user_dismissed_announcements`` — per-user dismissal join with
  composite PK ``(user_id, announcement_id)`` for idempotent writes.
  Both FKs ON DELETE CASCADE so dropping either side cleans the join.

No backfill — both tables start empty. The whole substrate is inert
until an operator posts a row via ``POST /api/v1/admin/announcements``.

``end_at > start_at`` is enforced at the Pydantic schema layer (see
``app/schemas/announcement.py``). We deliberately do NOT add a
``CHECK`` constraint here: MySQL only began enforcing CHECK in 8.0.16,
the suite covers SQLite for unit tests where CHECK syntax differs, and
the spec's schedule-validation contract is bidirectional (create + edit)
so the right enforcement layer is the request schema, not the column.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "049_announcements"
down_revision = "048_users_delete_perm"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "announcements",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "severity",
            sa.Enum(
                "info",
                "promo",
                "maintenance",
                name="announcement_severity",
                values_callable=lambda x: list(x),
            ),
            nullable=False,
            server_default="info",
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("start_at", sa.DateTime(), nullable=True),
        sa.Column("end_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_announcements_active_window",
        "announcements",
        ["is_active", "start_at", "end_at"],
    )

    op.create_table(
        "user_dismissed_announcements",
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "announcement_id",
            sa.Integer(),
            sa.ForeignKey("announcements.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "dismissed_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("user_dismissed_announcements")
    op.drop_index(
        "ix_announcements_active_window",
        table_name="announcements",
    )
    op.drop_table("announcements")
