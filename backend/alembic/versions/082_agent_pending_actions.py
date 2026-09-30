"""Agent pending actions: the preview-confirm table (TBD-577).

Revision ID: 082_agent_pending_actions
Revises: 081_org_primary_currency
Create Date: 2026-09-30

One new table, ``agent_pending_actions``. Creates, destroys nothing. See
``app/models/agent_pending_action.py`` for the semantics.

⚠ ``api_token_id`` is ``BigInteger`` for the reason migration 079 spells out
(MySQL rejects an FK whose type differs from ``api_tokens.id``), and every FK
is added AFTER the index that covers it so the constraint adopts the index the
ORM model names instead of MySQL auto-creating one under another name.

``downgrade`` drops the table; its FKs and indexes go with it, so the errno
1553 ordering trap of 079 does not apply.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "082_agent_pending_actions"
down_revision: Union[str, None] = "081_org_primary_currency"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_T = "agent_pending_actions"


def upgrade() -> None:
    op.create_table(
        _T,
        sa.Column("id", sa.CHAR(32), primary_key=True),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "channel", sa.Enum("in_app", "mcp", name="agent_action_channel"), nullable=False
        ),
        sa.Column(
            "api_token_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True
        ),
        sa.Column("tool", sa.String(64), nullable=False),
        sa.Column(
            "risk", sa.Enum("write", "sensitive", name="agent_action_risk"), nullable=False
        ),
        sa.Column(
            "mode", sa.Enum("confirm", "auto", name="agent_action_mode"), nullable=False,
        ),
        sa.Column("args_json", sa.JSON(), nullable=False),
        sa.Column("args_sha256", sa.CHAR(64), nullable=False),
        sa.Column("fingerprint", sa.CHAR(64), nullable=False),
        sa.Column("preview_json", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending", "executing", "done", "failed", "stale", "cancelled",
                name="agent_action_status",
            ),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
    )
    # Indexes FIRST so each FK adopts one (see module docstring).
    op.create_index("ix_agent_pending_actions_org_created", _T, ["org_id", "created_at"])
    op.create_index("ix_agent_pending_actions_user_id", _T, ["user_id"])
    op.create_index("ix_agent_pending_actions_api_token_id", _T, ["api_token_id"])
    op.create_foreign_key(
        "fk_agent_pending_actions_org_id", _T, "organizations", ["org_id"], ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_agent_pending_actions_user_id", _T, "users", ["user_id"], ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_agent_pending_actions_api_token_id", _T, "api_tokens", ["api_token_id"], ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_table(_T)
