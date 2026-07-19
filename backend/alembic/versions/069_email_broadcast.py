"""Create email_broadcasts + email_broadcast_recipients tables.

Revision ID: 069_email_broadcast
Revises: 068_dashboard_layout
Create Date: 2026-07-18

Superadmin email broadcast (spec 2026-07-18). Two tables:

- ``email_broadcasts``: one row per authored broadcast. ``segment`` is a
  plain ``String(32)`` (app-validated, NOT a DB enum — Ruling 4, dodges the
  MySQL ALTER-ENUM landmine on the axis designed to grow). ``status`` is a
  closed set and stays a native MySQL ENUM via ``sa.Enum(..., name=...)``,
  matching the ``announcement_severity`` convention in migration 0XX for
  ``announcements``.
- ``email_broadcast_recipients``: one row per targeted user, materialized at
  send time. ``status`` is likewise a native ENUM (``broadcast_recipient_status``).
  ``UNIQUE(broadcast_id, user_id)`` dedupes materialization inserts;
  ``INDEX(broadcast_id, status)`` serves the drain's pending-row select.

Verified up/down on real MySQL (isolated ``-p team-*`` stack) per spec
Ruling 5 — SQLite CI green does not prove MySQL enum DDL.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "069_email_broadcast"
down_revision = "068_dashboard_layout"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_broadcasts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("body_template", sa.Text(), nullable=False),
        sa.Column("segment", sa.String(32), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "sending",
                "completed",
                "failed",
                name="broadcast_status",
            ),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("total_recipients", sa.Integer(), nullable=True),
        sa.Column("sent_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dry_run_sent_at", sa.DateTime(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_email_broadcasts_created_by",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "email_broadcast_recipients",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("broadcast_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("first_name", sa.String(100), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "sent",
                "failed",
                "skipped",
                name="broadcast_recipient_status",
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["broadcast_id"],
            ["email_broadcasts.id"],
            name="fk_email_broadcast_recipients_broadcast",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_email_broadcast_recipients_user",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "broadcast_id", "user_id", name="uq_broadcast_recipient"
        ),
    )
    op.create_index(
        "ix_broadcast_recipient_status",
        "email_broadcast_recipients",
        ["broadcast_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_broadcast_recipient_status", table_name="email_broadcast_recipients"
    )
    op.drop_table("email_broadcast_recipients")
    op.drop_table("email_broadcasts")
    # MySQL native ENUM types created inline with the column are dropped
    # implicitly with the table (unlike Postgres, which needs an explicit
    # sa.Enum(...).drop(...) call). No further cleanup needed.
