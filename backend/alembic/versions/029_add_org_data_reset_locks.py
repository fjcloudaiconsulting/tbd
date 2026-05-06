"""Add org_data_reset_locks table — per-org guard against concurrent resets.

Revision ID: 029_reset_locks
Revises: 028_plan_features
Create Date: 2026-05-06

PR #134 follow-up: the reset endpoint commits per batch to avoid wedging
MySQL; without a server-side lock, two concurrent reset submissions can
interleave and (because account_types / categories have no DB-level
uniqueness on system slugs) duplicate the seeded defaults.

This table provides a single-row-per-org exclusive lease. The endpoint
acquires it before starting reset (rejecting with 409 if already held)
and releases it in finally. A staleness window (30 min) guards against
locks left orphaned by a crashed worker.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "029_reset_locks"
down_revision = "028_plan_features"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "org_data_reset_locks",
        sa.Column("org_id", sa.Integer, primary_key=True),
        sa.Column("acquired_by_user_id", sa.Integer, nullable=False),
        sa.Column("acquired_at", sa.DateTime, nullable=False),
        # Per-acquire lease token. The release path requires WHERE
        # token = :acquired_token so a stale-takeover can't be
        # accidentally released by the original (long-stalled) caller
        # — that would otherwise reopen the concurrent-reset window
        # this whole table exists to close.
        sa.Column("lease_token", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["acquired_by_user_id"], ["users.id"], ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    op.drop_table("org_data_reset_locks")
