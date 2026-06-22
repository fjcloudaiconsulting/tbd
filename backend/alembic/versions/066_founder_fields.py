"""Founding-members: add users.is_founder + users.last_active_at.

Revision ID: 066_founder_fields
Revises: 8e83c1dbe51b
Create Date: 2026-06-22

``is_founder`` server_default ``"1"`` grandfathers every existing user as
a founding member (the pre-launch testers are the most-founding members).
``last_active_at`` is NULL until first stamped by get_current_user. Soft
cap (1000 is a marketing number) — no gating at registration. Spec:
specs/2026-06-22-w1-quick-wins-design.md (W1b).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "066_founder_fields"
down_revision = "8e83c1dbe51b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_founder", sa.Boolean(), nullable=False, server_default="1"),
    )
    op.add_column(
        "users",
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "last_active_at")
    op.drop_column("users", "is_founder")
