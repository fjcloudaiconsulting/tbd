"""OpenAI-compatible base URL as the versioned API root (TBD-590).

Revision ID: 084_ai_credential_api_root
Revises: 083_modular_entitlements
Create Date: 2026-09-30

Adds ``org_ai_credentials.base_url_is_api_root`` (BOOL NOT NULL, server
default 0). Every existing row reads 0, which keeps the legacy
``{base_url}/v1`` root, so its request URLs stay byte-identical. No stored
``base_url`` is rewritten. Rows created after this revision store 1 and use
``base_url`` as the API root including its version.

``downgrade`` drops the column and is LOSSY: rows created after the upgrade
(for example OpenRouter ``https://openrouter.ai/api/v1`` or Gemini
``.../v1beta/openai``) go back to ``/v1`` appending and fail validation until
the revision is re-applied or the credentials are re-entered.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "084_ai_credential_api_root"
down_revision: Union[str, None] = "083_modular_entitlements"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "org_ai_credentials",
        sa.Column(
            "base_url_is_api_root",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("org_ai_credentials", "base_url_is_api_root")
