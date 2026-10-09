"""Session, single-use token and lease tables: sessions move to MySQL (INFRA-122).

Revision ID: 089_sessions_to_mysql
Revises: 088_mcp_oauth
Create Date: 2026-10-09

Additive only. ``downgrade`` drops the tables (the state is disposable: every
user signs in again).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql


revision: str = "089_sessions_to_mysql"
down_revision: Union[str, None] = "088_mcp_oauth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _id(n: int = 64):
    return sa.String(n).with_variant(mysql.VARCHAR(n, charset="ascii", collation="ascii_bin"), "mysql")


def _ts():
    return sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    op.create_table(
        "auth_session_families",
        sa.Column("sid", _id(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("head_jti", _id(), nullable=False),
        sa.Column("rotations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", _ts(), nullable=False),
    )
    op.create_index("ix_auth_session_families_expires_at", "auth_session_families", ["expires_at"])
    op.create_table(
        "auth_session_members",
        sa.Column("jti", _id(), primary_key=True),
        sa.Column(
            "sid",
            _id(),
            sa.ForeignKey("auth_session_families.sid", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("created_at", _ts(), nullable=False),
        sa.UniqueConstraint("sid", "seq", name="uq_auth_session_members_sid_seq"),
    )
    op.create_table(
        "used_tokens",
        sa.Column("scope", _id(32), primary_key=True),
        sa.Column("token", sa.CHAR(64), primary_key=True),
        sa.Column("expires_at", _ts(), nullable=False),
    )
    op.create_index("ix_used_tokens_expires_at", "used_tokens", ["expires_at"])
    op.create_table(
        "leases",
        sa.Column("name", _id(), primary_key=True),
        sa.Column("holder", _id(32), nullable=False),
        sa.Column("expires_at", _ts(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("leases")
    op.drop_table("used_tokens")
    op.drop_table("auth_session_members")
    op.drop_table("auth_session_families")
