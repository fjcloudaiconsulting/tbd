"""Platform AI model catalog: daily OpenRouter sync target (TBD-631).

Revision ID: 090_platform_ai_models
Revises: 089_sessions_to_mysql
Create Date: 2026-10-15

Additive only (one new table). ``downgrade`` drops it: the next sync rebuilds every
column except ``enabled``, which nothing can set until the switch-over ticket.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql


revision: str = "090_platform_ai_models"
down_revision: Union[str, None] = "089_sessions_to_mysql"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _id(n: int = 64):
    return sa.String(n).with_variant(mysql.VARCHAR(n, charset="ascii", collation="ascii_bin"), "mysql")


def _ts():
    return sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    op.create_table(
        "platform_ai_models",
        sa.Column("openrouter_id", _id(128), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("context_length", sa.Integer(), nullable=False),
        sa.Column("max_completion_tokens", sa.Integer(), nullable=True),
        sa.Column("supports_structured_outputs", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("headline_prompt_micros_per_mtok", sa.BigInteger(), nullable=True),
        sa.Column("headline_completion_micros_per_mtok", sa.BigInteger(), nullable=True),
        sa.Column("zdr_prompt_micros_per_mtok", sa.BigInteger(), nullable=True),
        sa.Column("zdr_completion_micros_per_mtok", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.Date(), nullable=True),
        sa.Column("last_seen_at", _ts(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_table("platform_ai_models")
