"""Plans simulation sandbox substrate, the ``scenarios`` table.

Revision ID: 052_scenarios
Revises: 051_reports_v2_substrate
Create Date: 2026-05-22

Creates the ``scenarios`` table that backs the Plans page (spec
``specs/2026-05-22-plans-page-simulation-sandbox.md``).

Architect-locked rules baked into this migration:

- Internal name = ``scenarios``. The user-facing label is "Plans";
  the word "scenario" never appears in the UI but the DB / model /
  router prefix all use it.
- Per-user. Plans are private to the creator by default. A future
  ``visibility`` column would flip per-org sharing on; out of scope
  here.
- Org-scoped enforcement at every query (matches the rest of the
  codebase). Both ``org_id`` and ``user_id`` are NOT NULL.
- ``params_json`` is a JSON blob validated by a Pydantic
  discriminated union on ``scenario_type``. Reads are loose; writes
  validate.
- ``projection_json`` caches the last computed projection so the
  list view can render a sparkline / verdict without re-running the
  engine.
- ``horizon_months`` defaults to 24. The DB column allows up to 480
  at the storage layer; the per-``scenario_type`` ceiling (120 for
  trip/purchase/custom, 480 for retirement) is enforced at the
  Pydantic validator on the simulate-request payload AND on
  scenario create / patch.
- ``is_active`` is the soft-delete flag, matching the rest of the
  codebase.

No backfill: the table starts empty. The whole substrate is inert
until a user creates a scenario via ``POST /api/v1/scenarios``.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "052_scenarios"
down_revision = "051_reports_v2_substrate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scenarios",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "org_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "scenario_type",
            sa.Enum(
                "trip",
                "purchase",
                "retirement",
                "custom",
                name="scenario_type",
                values_callable=lambda x: list(x),
            ),
            nullable=False,
        ),
        sa.Column("params_json", sa.JSON(), nullable=False),
        sa.Column("projection_json", sa.JSON(), nullable=True),
        sa.Column("projection_engine", sa.String(length=40), nullable=True),
        sa.Column("projection_computed_at", sa.DateTime(), nullable=True),
        sa.Column(
            "horizon_months",
            sa.Integer(),
            nullable=False,
            server_default="24",
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_scenarios_org_user",
        "scenarios",
        ["org_id", "user_id"],
    )
    op.create_index(
        "ix_scenarios_org_active",
        "scenarios",
        ["org_id", "is_active"],
    )


def downgrade() -> None:
    op.drop_index("ix_scenarios_org_active", table_name="scenarios")
    op.drop_index("ix_scenarios_org_user", table_name="scenarios")
    op.drop_table("scenarios")
