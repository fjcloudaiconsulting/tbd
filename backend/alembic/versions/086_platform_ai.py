"""Platform AI kernel: platform_provider column + global spend row (TBD-586).

Revision ID: 086_platform_ai
Revises: 085_rate_limits
Create Date: 2026-10-08

Additive. ``downgrade`` is LOSSY: it deletes every platform credential row
first (their routing rows cascade; ledger rows keep ``billing_source='platform'``
with ``credential_id`` set NULL), then drops the table, constraint and column.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "086_platform_ai"
down_revision: Union[str, None] = "085_rate_limits"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PLATFORM_PROVIDER = sa.Enum(
    "openrouter", "openai", "anthropic", "gemini", name="ai_platform_provider"
)


def upgrade() -> None:
    op.add_column(
        "org_ai_credentials",
        sa.Column("platform_provider", PLATFORM_PROVIDER, nullable=True),
    )
    op.create_unique_constraint(
        "uq_org_ai_credentials_org_platform",
        "org_ai_credentials",
        ["org_id", "platform_provider"],
    )
    op.create_table(
        "platform_ai_spend",
        sa.Column("period_start", sa.Date(), primary_key=True),
        sa.Column("cents", sa.BigInteger(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.execute("DELETE FROM org_ai_credentials WHERE platform_provider IS NOT NULL")
    op.drop_table("platform_ai_spend")
    op.drop_constraint(
        "uq_org_ai_credentials_org_platform", "org_ai_credentials", type_="unique"
    )
    op.drop_column("org_ai_credentials", "platform_provider")
