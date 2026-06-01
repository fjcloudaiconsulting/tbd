"""report version history (max 5, original pinned)

Replaces the single ``reports.original_*`` snapshot columns with a
bounded ``report_versions`` history table. Backfills one
``is_original=True`` version per existing report (from the original
snapshot columns, falling back to live state), then drops the snapshot
columns.

Revision ID: 064_report_versions
Revises: 063_reports_original_snapshot
"""
from alembic import op
import sqlalchemy as sa

revision = "064_report_versions"
down_revision = "063_reports_original_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "report_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("is_original", sa.Boolean(), nullable=False),
        sa.Column("layout_json", sa.JSON(), nullable=True),
        sa.Column("canvas_filters_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["reports.id"],
            name="fk_report_versions_report",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_report_versions_report", "report_versions", ["report_id"]
    )

    # Backfill one original version per existing report. Prefer the
    # snapshot columns; fall back to live layout/filters when NULL.
    op.execute(
        "INSERT INTO report_versions "
        "(report_id, is_original, layout_json, canvas_filters_json, created_at) "
        "SELECT id, TRUE, "
        "COALESCE(original_layout_json, layout_json), "
        "COALESCE(original_canvas_filters_json, canvas_filters_json), "
        "created_at "
        "FROM reports"
    )

    op.drop_column("reports", "original_canvas_filters_json")
    op.drop_column("reports", "original_layout_json")


def downgrade() -> None:
    op.add_column(
        "reports",
        sa.Column("original_layout_json", sa.JSON(), nullable=True),
    )
    op.add_column(
        "reports",
        sa.Column("original_canvas_filters_json", sa.JSON(), nullable=True),
    )
    # Best-effort: copy the is_original version back into the columns.
    op.execute(
        "UPDATE reports SET "
        "original_layout_json = ("
        "  SELECT rv.layout_json FROM report_versions rv "
        "  WHERE rv.report_id = reports.id AND rv.is_original = TRUE "
        "  LIMIT 1"
        "), "
        "original_canvas_filters_json = ("
        "  SELECT rv.canvas_filters_json FROM report_versions rv "
        "  WHERE rv.report_id = reports.id AND rv.is_original = TRUE "
        "  LIMIT 1"
        ")"
    )
    op.drop_index("ix_report_versions_report", table_name="report_versions")
    op.drop_table("report_versions")
