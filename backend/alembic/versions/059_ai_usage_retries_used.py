"""AI usage ledger: add retries_used column (PR3 of AI tier train).

Revision ID: 059_ai_usage_retries_used
Revises: 058_ai_usage_ledger
Create Date: 2026-05-22

Architect lock #13 for ``StructuredOutputCapable`` caps retries at 2
on JSON parse / schema-validation failure (3 total attempts) before
``STATUS_ERROR_STRUCTURED_OUTPUT``. Ops needs the per-row count so a
forensic query like

  SELECT feature_key, AVG(retries_used)
  FROM ai_usage_ledger
  WHERE model = 'ollama-llama3'
  GROUP BY feature_key;

surfaces a model whose structured output is borderline-failing — that
warning has to land before the cap eats meaningful budget.

The column is NOT NULL with a server_default of 0 so historical rows
(chat / embed / stream that have no retry concept) keep the
sentinel 0. Only ``call_llm_structured`` writes a non-zero value.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "059_ai_usage_retries_used"
down_revision = "058_ai_usage_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_usage_ledger",
        sa.Column(
            "retries_used",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("ai_usage_ledger", "retries_used")
