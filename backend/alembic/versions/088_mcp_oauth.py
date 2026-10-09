"""MCP OAuth 2.1 authorization server: oauth_clients + grant columns (TBD-587).

Revision ID: 088_mcp_oauth
Revises: 087_schema_convergence
Create Date: 2026-10-09

Additive. ``oauth_clients`` holds dynamically registered public clients. An
OAuth grant is one ``api_tokens`` row: new nullable columns carry the client,
the rotating refresh token (current and previous hash), its expiry, and the
authorization code issued at consent (hash, PKCE challenge, redirect hash).

The FK needs ``oauth_clients.id`` and ``api_tokens.oauth_client_id`` in one
charset/collation, so the table is created in ``api_tokens``' own collation
(an added column inherits its table's default).

Downgrade drops the columns, the FK and the table, which loses every OAuth
grant and client (acceptable for an additive feature: users re-connect).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "088_mcp_oauth"
down_revision: Union[str, None] = "087_schema_convergence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FK = "fk_api_tokens_oauth_client"
INDEXES = (
    ("ix_api_tokens_oauth_client_id", "oauth_client_id", False),
    ("ix_api_tokens_refresh_hash", "refresh_hash", True),
    ("ix_api_tokens_refresh_prev_hash", "refresh_prev_hash", False),
    ("ix_api_tokens_code_hash", "code_hash", True),
)
COLUMNS = (
    "oauth_client_id", "refresh_hash", "refresh_prev_hash", "refresh_expires_at",
    "code_hash", "code_challenge", "code_redirect_hash",
)


def _table_options() -> dict[str, str]:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return {}
    collation = bind.execute(sa.text(
        "SELECT TABLE_COLLATION FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'api_tokens'"
    )).scalar_one()
    return {"mysql_charset": collation.split("_", 1)[0], "mysql_collate": collation}


def upgrade() -> None:
    op.create_table(
        "oauth_clients",
        sa.Column("id", sa.CHAR(32), nullable=False),
        sa.Column("client_name", sa.String(100), nullable=False),
        sa.Column("redirect_uris", sa.JSON(), nullable=False),
        sa.Column("metadata_key", sa.CHAR(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        **_table_options(),
    )
    op.create_index("ix_oauth_clients_metadata_key", "oauth_clients", ["metadata_key"], unique=True)

    op.add_column("api_tokens", sa.Column("oauth_client_id", sa.CHAR(32), nullable=True))
    op.add_column("api_tokens", sa.Column("refresh_hash", sa.String(64), nullable=True))
    op.add_column("api_tokens", sa.Column("refresh_prev_hash", sa.String(64), nullable=True))
    op.add_column("api_tokens", sa.Column("refresh_expires_at", sa.DateTime(), nullable=True))
    op.add_column("api_tokens", sa.Column("code_hash", sa.String(64), nullable=True))
    op.add_column("api_tokens", sa.Column("code_challenge", sa.String(64), nullable=True))
    op.add_column("api_tokens", sa.Column("code_redirect_hash", sa.CHAR(64), nullable=True))
    for name, column, unique in INDEXES:
        op.create_index(name, "api_tokens", [column], unique=unique)
    op.create_foreign_key(
        FK, "api_tokens", "oauth_clients", ["oauth_client_id"], ["id"], ondelete="RESTRICT"
    )


def downgrade() -> None:
    # FK first: MySQL refuses to drop the index that covers it (errno 1553).
    op.drop_constraint(FK, "api_tokens", type_="foreignkey")
    for name, _column, _unique in INDEXES:
        op.drop_index(name, table_name="api_tokens")
    for column in COLUMNS:
        op.drop_column("api_tokens", column)
    op.drop_table("oauth_clients")
