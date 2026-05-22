"""Per-org / per-user rate limit overrides (L4.10).

Revision ID: 060_rate_limit_overrides
Revises: 059_ai_usage_retries_used
Create Date: 2026-05-22

Creates ``rate_limit_overrides`` so superadmins can adjust the
per-request budget for a specific org or user without redeploying.

Shape choice. A dedicated row-per-override table (vs. a JSON blob on
``org_settings``) is taken so the admin UI can list, sort, and filter
across every override on the platform via cheap SQL. Each row carries
exactly one scope: either a non-NULL ``org_id`` and NULL ``user_id``,
or the other way around. CHECK-style integrity at the row level is
enforced upstream by the service (one-of, never both) — MySQL 8.0.16+
supports CHECK constraints, but the project's SQLite test substrate
behaves differently and the service layer is the single write surface
anyway, so keeping the invariant in code is more portable than spread
across two DB dialects.

Lookup paths the indexes cover:

- ``(user_id, endpoint_pattern)`` — per-user resolve.
- ``(org_id, endpoint_pattern)`` — per-org resolve.

Both are partial in spirit (one column is always NULL) but MySQL does
not support partial indexes; the composite still answers the read in
one seek because the leading column eliminates the irrelevant scope.

``endpoint_pattern`` is an opaque string set by the application — a
short identifier shared between ``rate_limit_overrides_service`` and
the limiter call site (e.g. ``auth.login``, ``auth.register``,
``reports.list``). Not a regex / glob. The string is intentionally
short so the index column width stays small.

``created_by`` is ``ON DELETE SET NULL`` so deleting the authoring
superadmin leaves the override (and its audit history) intact.

``expires_at`` is nullable. NULL means "never expires"; non-NULL means
the resolver treats the row as if absent once ``now() >= expires_at``.
The check is service-side (combined with the cache invalidation path)
because the cache key set is small and TTL-aligned.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "060_rate_limit_overrides"
down_revision = "059_ai_usage_retries_used"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_overrides",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "org_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("endpoint_pattern", sa.String(length=80), nullable=False),
        sa.Column("max_requests", sa.Integer(), nullable=False),
        # period_seconds is a finite positive integer. The service maps
        # this to slowapi's "N/period" string at resolve time. Storing
        # seconds (vs. a string like "1/minute") keeps comparisons /
        # bounds-checks in the service / admin UI numeric.
        sa.Column("period_seconds", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("note", sa.Text(), nullable=True),
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
        "ix_rate_limit_overrides_user_endpoint",
        "rate_limit_overrides",
        ["user_id", "endpoint_pattern"],
    )
    op.create_index(
        "ix_rate_limit_overrides_org_endpoint",
        "rate_limit_overrides",
        ["org_id", "endpoint_pattern"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_rate_limit_overrides_org_endpoint",
        table_name="rate_limit_overrides",
    )
    op.drop_index(
        "ix_rate_limit_overrides_user_endpoint",
        table_name="rate_limit_overrides",
    )
    op.drop_table("rate_limit_overrides")
