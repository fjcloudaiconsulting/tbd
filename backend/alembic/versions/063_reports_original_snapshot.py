"""add reports original snapshot columns

Revision ID: 063_reports_original_snapshot
Revises: 062_ollama_nullable_api_key
"""
from alembic import op
import sqlalchemy as sa

revision = "063_reports_original_snapshot"
down_revision = "062_ollama_nullable_api_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reports", sa.Column("original_layout_json", sa.JSON(), nullable=True))
    op.add_column("reports", sa.Column("original_canvas_filters_json", sa.JSON(), nullable=True))
    op.execute(
        "UPDATE reports SET original_layout_json = layout_json, "
        "original_canvas_filters_json = canvas_filters_json"
    )


def downgrade() -> None:
    op.drop_column("reports", "original_canvas_filters_json")
    op.drop_column("reports", "original_layout_json")
