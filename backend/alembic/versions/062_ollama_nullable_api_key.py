"""Make org_ai_credentials.encrypted_api_key nullable (Ollama LAN-only mode).

Also makes last_four and key_fingerprint nullable so Ollama-no-key rows
can store NULL there instead of a misleading empty string.

Revision ID: 062_ollama_nullable_api_key
Revises: 061_cc_payment_day_columns
Create Date: 2026-05-29

Spec: specs/2026-05-22-ai-tier-byo-and-native-providers.md line 37 + ~L219
declares Ollama-no-key (LAN-only homelab mode) as a supported configuration.
Implementation never caught up: ``encrypted_api_key`` was NOT NULL so a POST
without an api_key would 422 at the schema layer.

Changes: loosen ``encrypted_api_key``, ``last_four``, and ``key_fingerprint``
from NOT NULL to nullable.

Downgrade re-adds NOT NULL. No backfill needed — pre-launch state, zero real
Ollama-no-key rows exist.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "062_ollama_nullable_api_key"
down_revision = "061_cc_payment_day_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "org_ai_credentials",
        "encrypted_api_key",
        existing_type=sa.Text(),
        nullable=True,
    )
    op.alter_column(
        "org_ai_credentials",
        "last_four",
        existing_type=sa.String(length=8),
        nullable=True,
    )
    op.alter_column(
        "org_ai_credentials",
        "key_fingerprint",
        existing_type=sa.String(length=64),
        nullable=True,
    )


def downgrade() -> None:
    # Pre-launch: no real Ollama-no-key rows expected; safe to re-add NOT NULL.
    op.alter_column(
        "org_ai_credentials",
        "encrypted_api_key",
        existing_type=sa.Text(),
        nullable=False,
    )
    op.alter_column(
        "org_ai_credentials",
        "last_four",
        existing_type=sa.String(length=8),
        nullable=False,
    )
    op.alter_column(
        "org_ai_credentials",
        "key_fingerprint",
        existing_type=sa.String(length=64),
        nullable=False,
    )
