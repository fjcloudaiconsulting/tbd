"""Per-org votes on the shared merchant dictionary (BE-28).

Revision ID: 097_merchant_dictionary_votes
Revises: 096_ai_provider_varchar
Create Date: 2026-10-25

One row per (dictionary entry, org): an org's vote, counted once, when it
confirms the server's own dictionary suggestion. ``merchant_dictionary.vote_count``
goes up by one only when such a row is created.

Additive only, with no data change. Stored ``vote_count`` values cannot be
attributed to an org, so they are left as they are. ``downgrade`` drops the
table; the counts it added stay in ``vote_count``.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "097_merchant_dictionary_votes"
down_revision: Union[str, None] = "096_ai_provider_varchar"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "merchant_dictionary_votes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "entry_id",
            sa.Integer(),
            sa.ForeignKey("merchant_dictionary.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "org_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "entry_id", "org_id", name="uq_merchant_dictionary_votes_entry_org"
        ),
    )
    op.create_index(
        "ix_merchant_dictionary_votes_org_id", "merchant_dictionary_votes", ["org_id"]
    )


def downgrade() -> None:
    op.drop_table("merchant_dictionary_votes")
