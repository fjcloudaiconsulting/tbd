"""Rate limit counters table: rate limits move to MySQL (INFRA-121).

Revision ID: 085_rate_limits
Revises: 084_ai_credential_api_root
Create Date: 2026-10-05

Additive only. ``downgrade`` drops the table (counters are disposable).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "085_rate_limits"
down_revision: Union[str, None] = "084_ai_credential_api_root"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rate_limits",
        sa.Column("key", sa.CHAR(64), primary_key=True),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Double(), nullable=False),
    )
    op.create_index("ix_rate_limits_expires_at", "rate_limits", ["expires_at"])


def downgrade() -> None:
    op.drop_table("rate_limits")
