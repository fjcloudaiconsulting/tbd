"""Add audit_events table — durable superadmin audit log (L4.7).

Revision ID: 030_audit_events
Revises: 029_reset_locks
Create Date: 2026-05-06

Persists the structured ``admin.org.*`` and ``org.data.*`` events
that already stream to structlog into a durable, queryable store.

Foreign keys use ON DELETE SET NULL on both actor_user_id and
target_org_id so the audit history outlives the rows it describes
(otherwise wiping an org would erase the record of who wiped it,
which is the opposite of what an audit log is for). Snapshot
columns (actor_email, target_org_name) preserve the human-readable
identity at event time.

created_at uses DATETIME(6) with NOW(6) default to give microsecond
precision — events from the same admin click can land within the
same second, and we still want a stable order in the UI.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "030_audit_events"
down_revision = "029_reset_locks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("actor_user_id", sa.Integer, nullable=True),
        sa.Column("actor_email", sa.String(255), nullable=False),
        sa.Column("target_org_id", sa.Integer, nullable=True),
        sa.Column("target_org_name", sa.String(200), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column(
            "outcome",
            sa.Enum("success", "failure", name="auditoutcome"),
            nullable=False,
        ),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["target_org_id"],
            ["organizations.id"],
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_audit_events_event_type", "audit_events", ["event_type"]
    )
    op.create_index(
        "ix_audit_events_actor_user_id", "audit_events", ["actor_user_id"]
    )
    op.create_index(
        "ix_audit_events_target_org_id", "audit_events", ["target_org_id"]
    )
    op.create_index(
        "ix_audit_events_created_at", "audit_events", ["created_at"]
    )


def downgrade() -> None:
    # drop_table removes FKs and their backing indexes implicitly on
    # MySQL — listing the indexes individually first fails because
    # MySQL refuses to drop an index that an FK still depends on.
    op.drop_table("audit_events")
