"""Add 'native' to the ai_provider enum (PR1 follow-up).

Revision ID: 054_ai_provider_enum_native
Revises: 053_org_ai_consents
Create Date: 2026-05-22

PR1 ships the native adapter scaffolding (gated, default off). The
service layer refuses credential creation for native, so no row will
hit the column in PR1 — but expanding the enum here means PR4 doesn't
have to choreograph a backend deploy + migration sequence when it
flips ``AI_NATIVE_ENABLED=true``.

MySQL stores enums inline with the column; PostgreSQL needs an
ALTER TYPE. Both are handled.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text


revision = "054_ai_provider_enum_native"
down_revision = "053_org_ai_consents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute(
            text(
                "ALTER TABLE org_ai_credentials "
                "MODIFY COLUMN provider "
                "ENUM('openai','anthropic','ollama','openai_compatible','native') "
                "NOT NULL"
            )
        )
    elif bind.dialect.name == "postgresql":
        # PostgreSQL needs ALTER TYPE outside a transaction in older
        # versions; emit a no-op-safe ADD VALUE.
        op.execute(
            text("ALTER TYPE ai_provider ADD VALUE IF NOT EXISTS 'native'")
        )
    # SQLite stores enums as strings + a CHECK — the test fixtures
    # rebuild the schema each run, so no-op here.


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute(
            text(
                "ALTER TABLE org_ai_credentials "
                "MODIFY COLUMN provider "
                "ENUM('openai','anthropic','ollama','openai_compatible') "
                "NOT NULL"
            )
        )
    # PostgreSQL doesn't support removing an enum value cleanly;
    # downgrade is a no-op there. Same for SQLite.
