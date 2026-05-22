"""AI usage ledger (PR2 of AI tier train).

Revision ID: 058_ai_usage_ledger
Revises: 057_ai_provider_enum_native
Create Date: 2026-05-22

PR2 of the AI tier rollout wires the dispatch chokepoint
(``call_llm``) with cap enforcement + the per-call ledger. This
migration creates the ledger table; the cap tables themselves landed
in 055.

Indexes mirror the read patterns in spec §7 / the PR2 brief:

- ``(org_id, dispatched_at)`` — feeds the rolling-window org-wide
  cap query (``SUM(est_cost_cents) WHERE org_id=? AND dispatched_at
  BETWEEN month_start AND now``).
- ``(org_id, feature_key, dispatched_at)`` — same shape for the
  per-feature cap query.

FK shape note: the ledger uses a single-column FK on
``credential_id`` (ON DELETE SET NULL) rather than the composite
``(org_id, credential_id)`` pattern used by the routing tables.
Reason: MySQL refuses ON DELETE SET NULL on a composite FK where
``org_id`` is NOT NULL (errno 1830). Cross-org integrity for the
ledger is enforced UPSTREAM — every ledger row is written by the
dispatch chokepoint (``ai_dispatch.call_llm``) using a credential
the routing FK already pinned to the org, so a cross-org row is
unreachable through normal code. The column stays nullable + ON
DELETE SET NULL so a credential revoke leaves the historical row
intact (T13 forensic source).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "058_ai_usage_ledger"
down_revision = "057_ai_provider_enum_native"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_usage_ledger",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            nullable=False,
            autoincrement=True,
        ),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("credential_id", sa.Integer(), nullable=True),
        sa.Column("feature_key", sa.String(length=120), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column(
            "prompt_tokens",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "completion_tokens",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "total_tokens",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "est_cost_cents",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "dispatched_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "latency_ms",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "success",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("error_class", sa.String(length=120), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_ai_usage_ledger"),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_ai_usage_ledger_org",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["credential_id"],
            ["org_ai_credentials.id"],
            name="fk_ai_usage_ledger_cred",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_ai_usage_org_dispatched",
        "ai_usage_ledger",
        ["org_id", "dispatched_at"],
    )
    op.create_index(
        "ix_ai_usage_org_feature_dispatched",
        "ai_usage_ledger",
        ["org_id", "feature_key", "dispatched_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ai_usage_org_feature_dispatched",
        table_name="ai_usage_ledger",
    )
    op.drop_index(
        "ix_ai_usage_org_dispatched",
        table_name="ai_usage_ledger",
    )
    op.drop_table("ai_usage_ledger")
