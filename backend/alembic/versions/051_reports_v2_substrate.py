"""Reports v2 substrate — reports table + supporting transactions indexes.

Revision ID: 051_reports_v2_substrate
Revises: 050_notifications
Create Date: 2026-05-22

Creates:

- ``reports`` — owner-authored, optionally org-shared user reports
  (spec ``specs/2026-05-22-reports-v2-flexible-canvas.md``). Stores the
  layout + canvas-filter JSON; the AST endpoint reads no rows from here
  in PR1 (CRUD only).
  ``owner_user_id`` is ``ON DELETE RESTRICT``: per the spec §8 the
  service layer is the authoritative deletion gate, and the FK is the
  bright-line safety net for any code path that bypasses it.
- Composite transactions indexes that the AST compiler relies on
  (org_id + category_id + date and org_id + account_id + date). The
  third pattern from the spec — ``(org_id, date)`` — is already covered
  by migration 004's ``ix_transactions_org_date``.

Both tables / indexes ship inert behind ``FEATURE_REPORTS_V2`` until
the frontend lands; the migration is safe to apply ahead of the flag
flip because the indexes are pure read-side acceleration.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "051_reports_v2_substrate"
down_revision = "050_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reports",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "owner_user_id",
            sa.Integer(),
            # RESTRICT: service-layer enforces user-delete semantics
            # (section 8 of the spec — org-shared reports reassign to
            # org owner, private reports hard-delete). The FK is the
            # last line of defence against a code path that bypasses
            # the service.
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "org_id",
            sa.Integer(),
            sa.ForeignKey(
                "organizations.id",
                name="fk_reports_org",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column(
            "visibility",
            sa.Enum(
                "private",
                "org",
                name="report_visibility",
                values_callable=lambda x: list(x),
            ),
            nullable=False,
            server_default="private",
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("layout_json", sa.JSON(), nullable=False),
        sa.Column(
            "canvas_filters_json",
            sa.JSON(),
            nullable=False,
        ),
        sa.Column(
            "schema_version",
            sa.Integer(),
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
    # List page reads: filter by (org_id, visibility) to fetch the
    # "shared by your org" section.
    op.create_index(
        "ix_reports_org_visibility",
        "reports",
        ["org_id", "visibility"],
    )
    # "Yours" section: filter by owner_user_id alone.
    op.create_index(
        "ix_reports_owner",
        "reports",
        ["owner_user_id"],
    )

    # Composite transactions indexes that the AST compiler relies on.
    # ``ix_transactions_org_date`` (org_id, date) already ships via
    # migration 004 — we only add the two newly required composites.
    op.create_index(
        "ix_transactions_org_category_date",
        "transactions",
        ["org_id", "category_id", "date"],
    )
    op.create_index(
        "ix_transactions_org_account_date",
        "transactions",
        ["org_id", "account_id", "date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_transactions_org_account_date",
        table_name="transactions",
    )
    op.drop_index(
        "ix_transactions_org_category_date",
        table_name="transactions",
    )
    op.drop_index("ix_reports_owner", table_name="reports")
    op.drop_index("ix_reports_org_visibility", table_name="reports")
    op.drop_table("reports")
