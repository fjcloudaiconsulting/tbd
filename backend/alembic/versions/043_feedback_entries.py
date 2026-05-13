"""Add feedback_entries table for in-app feedback widget.

Revision ID: 043_feedback_entries
Revises: 042_users_onboarded_at
Create Date: 2026-05-13

Captures user-submitted feedback from the in-app widget. Spec captured
2026-05-08 in `project_inapp_feedback_widget.md`.

Two privacy decisions baked into the schema:

1. **Identity opt-in is column-level.** `user_id` and `org_id` are both
   nullable and ON DELETE SET NULL. The router only fills them when the
   submitter checks the "Include my account info so we can follow up"
   box, defaulting OFF. A row with NULL identity columns is the
   anonymous-shaped record we agreed to.

2. **Operational context is required and structured.** `context` JSON
   is NOT NULL because we want to be able to triage even an anonymous
   submission (path, viewport, user-agent, app-version, theme). The
   router strips query params from the URL before storing, and the
   service layer is the single normalization site.

Indexes:
   - `created_at` for chronological admin listing (post-L4.x).
   - `category` for filterable admin views.

Note: an admin UI to view feedback is intentionally out of scope here
(see project memory under "Out of scope (v1)"). The table is shaped
to support it when that PR lands without another migration.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision = "043_feedback_entries"
down_revision = "042_users_onboarded_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "feedback_entries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # Identity-opt-in fields. Both NULLABLE on purpose: anonymous
        # submissions store NULL; identified submissions store the FK
        # so an operator can follow up. ON DELETE SET NULL preserves
        # the feedback row when the user or org is later deleted (the
        # admin still wants to read the message; the link to a now-
        # gone user just becomes orphan-safe).
        sa.Column(
            "user_id", sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "org_id", sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "category",
            sa.Enum(
                "bug", "feature", "other",
                name="feedback_category",
                values_callable=lambda x: list(x),
            ),
            nullable=False,
        ),
        # JSON shape (router-controlled, no PII):
        #   { "url": "/transactions",   # query params stripped
        #     "user_agent": "...",
        #     "app_version": "1.2.3" | "dev",
        #     "viewport": {"w": 1440, "h": 900},
        #     "theme": "light" | "dark" }
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(),
            server_default=sa.func.now(), nullable=False,
            index=True,
        ),
    )
    op.create_index(
        "ix_feedback_entries_category",
        "feedback_entries",
        ["category"],
    )


def downgrade() -> None:
    op.drop_index("ix_feedback_entries_category", table_name="feedback_entries")
    op.drop_table("feedback_entries")
