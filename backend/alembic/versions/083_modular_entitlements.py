"""Modular entitlements: plan usage limits, counters, limit overrides (TBD-585).

Revision ID: 083_modular_entitlements
Revises: 082_agent_pending_actions
Create Date: 2026-09-30

1. ``plans.usage_limits`` JSON: added NULL, backfilled with the literal
   canonical dict below (passed as a dict, never ``json.dumps``, which would
   store a string scalar), self-checked on MySQL (``JSON_TYPE`` must be
   OBJECT on every row), then NOT NULL. On MySQL it also gets
   ``DEFAULT (JSON_OBJECT())`` like ``plans.features`` (028), so an insert by
   the previous release during a rolling deploy stores ``{}``, which the read
   path canonicalizes to the defaults.
2. ``usage_counters``: one row per (org, meter, period kind, period start).
3. ``org_limit_overrides``: sibling of ``org_feature_overrides``. Indexes are
   created BEFORE the FKs so each FK adopts the index the ORM model names
   (see 082).
4. ``ai_usage_ledger.billing_source``: every existing row is ``org_key``.

``downgrade`` drops all of it and is LOSSY: counters, limit overrides, plan
limits and the billing source are gone.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.sql import column, table


revision: str = "083_modular_entitlements"
down_revision: Union[str, None] = "082_agent_pending_actions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Literal on purpose: a migration must not change meaning when the catalog
# does. Pinned equal to ``PlanUsageLimits().model_dump(by_alias=True)`` today
# by tests/migrations/test_083_usage_limits_backfill.py.
CANONICAL_USAGE_LIMITS: dict = {
    "assistant.turns": {"period": "month", "limit": None},
    "mcp.calls": {"period": "month", "limit": None},
    "platform_ai.tokens": {"period": "month", "limit": 0},
    "platform_ai.cents": {"period": "month", "limit": 0},
}

_PERIOD = ("day", "month")


def backfill_usage_limits(conn) -> int:
    """Set every NULL ``plans.usage_limits`` to the canonical dict; return rows."""
    plans_t = table("plans", column("usage_limits", sa.JSON))
    return conn.execute(
        plans_t.update()
        .where(plans_t.c.usage_limits.is_(None))
        .values(usage_limits=CANONICAL_USAGE_LIMITS)
    ).rowcount


def assert_usage_limits_are_objects(conn) -> None:
    """MySQL only: refuse to go NOT NULL over a row that is not a JSON object."""
    bad = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM plans "
            "WHERE usage_limits IS NULL OR JSON_TYPE(usage_limits) <> 'OBJECT'"
        )
    ).scalar_one()
    if bad:
        raise RuntimeError(f"083: {bad} plans rows have a non-object usage_limits")


def upgrade() -> None:
    conn = op.get_bind()
    mysql = conn.dialect.name == "mysql"

    op.add_column("plans", sa.Column("usage_limits", sa.JSON(), nullable=True))
    backfill_usage_limits(conn)
    if mysql:
        assert_usage_limits_are_objects(conn)
    with op.batch_alter_table("plans") as batch:
        batch.alter_column(
            "usage_limits",
            existing_type=sa.JSON(),
            nullable=False,
            server_default=sa.text("(JSON_OBJECT())") if mysql else None,
        )

    op.create_table(
        "usage_counters",
        sa.Column(
            "org_id", sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("meter", sa.String(40), nullable=False),
        sa.Column("period", sa.Enum(*_PERIOD, name="usage_period"), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("value", sa.BigInteger(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("org_id", "meter", "period", "period_start"),
    )

    _T = "org_limit_overrides"
    op.create_table(
        _T,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("meter", sa.String(40), nullable=False),
        sa.Column("period", sa.Enum(*_PERIOD, name="usage_period"), nullable=False),
        sa.Column("limit_value", sa.BigInteger(), nullable=True),
        sa.Column("set_by", sa.Integer(), nullable=True),
        sa.Column("set_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.UniqueConstraint("org_id", "meter", name="uq_org_limit_meter"),
    )
    # Indexes FIRST so each FK adopts one (org_id is covered by the UNIQUE).
    op.create_index("ix_olo_expires_at", _T, ["expires_at"])
    op.create_index("ix_olo_set_by", _T, ["set_by"])
    with op.batch_alter_table(_T) as batch:
        batch.create_foreign_key(
            "fk_org_limit_overrides_org_id", "organizations", ["org_id"], ["id"],
            ondelete="CASCADE",
        )
        batch.create_foreign_key(
            "fk_org_limit_overrides_set_by", "users", ["set_by"], ["id"],
            ondelete="SET NULL",
        )

    op.add_column(
        "ai_usage_ledger",
        sa.Column(
            "billing_source",
            sa.Enum("org_key", "platform", name="ai_billing_source"),
            nullable=False,
            server_default="org_key",
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("ai_usage_ledger") as batch:
        batch.drop_column("billing_source")
    op.drop_table("org_limit_overrides")
    op.drop_table("usage_counters")
    with op.batch_alter_table("plans") as batch:
        batch.drop_column("usage_limits")
