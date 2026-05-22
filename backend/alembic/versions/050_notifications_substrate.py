"""Notification system substrate.

Revision ID: 050_notifications
Revises: 049_announcements
Create Date: 2026-05-22

Creates the two tables that back the notification system
(specs ``2026-05-21-notification-system-sensitive-ops.md`` +
``2026-05-22-notification-system-2nd-arch-pass.md``):

- ``notifications`` — per-user feed rows written by sensitive-op
  routes. ``seen_at`` + ``audit_event_id`` columns are included
  from this initial create per the 2nd-arch delta (G1 + G5). No
  separate follow-up migration.
- ``user_notification_preferences`` — one row per user, four
  categories x two channels. Lazy-created by the service layer on
  first read. ``email_security=True`` is the default; the API
  layer rejects ``email_security=False`` writes with 400.

No backfill. Both tables start empty. Existing users get
preference rows lazily on first GET. No notification rows are
generated for audit events that pre-date the rollout — see
2nd-arch delta G2.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "050_notifications"
down_revision = "049_announcements"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "category",
            sa.Enum(
                "security",
                "account",
                "org_admin",
                "org_activity",
                name="notification_category",
                values_callable=lambda x: list(x),
            ),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("link_url", sa.String(length=512), nullable=True),
        sa.Column("seen_at", sa.DateTime(), nullable=True),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.Column(
            "audit_event_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.ForeignKey("audit_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(6),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_notifications_user_unseen",
        "notifications",
        ["user_id", "seen_at", "created_at"],
    )
    op.create_index(
        "ix_notifications_user_unread",
        "notifications",
        ["user_id", "read_at", "created_at"],
    )
    op.create_index(
        "ix_notifications_event_type",
        "notifications",
        ["event_type"],
    )

    op.create_table(
        "user_notification_preferences",
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "email_security",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "email_account",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "email_org_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "email_org_activity",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "in_app_security",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "in_app_account",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "in_app_org_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "in_app_org_activity",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("user_notification_preferences")
    op.drop_index(
        "ix_notifications_event_type",
        table_name="notifications",
    )
    op.drop_index(
        "ix_notifications_user_unread",
        table_name="notifications",
    )
    op.drop_index(
        "ix_notifications_user_unseen",
        table_name="notifications",
    )
    op.drop_table("notifications")
