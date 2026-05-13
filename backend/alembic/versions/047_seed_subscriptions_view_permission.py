"""Seed subscriptions.view permission into the superadmin role (L4.5).

Revision ID: 047_subscriptions_view_perm
Revises: 046_users_view_perm
Create Date: 2026-05-13

L4.5 ships the subscription & revenue admin surface. It is gated by a
new ``subscriptions.view`` permission key (added to
``app/auth/permissions.py``'s ``Permission`` literal + ``ALL_PERMISSIONS``).

The runtime resolver short-circuits via ``is_superadmin``, so the seed
below is for parity with future non-superadmin roles and to keep the
``/admin/roles`` UI's permission editor accurate. We INSERT-IGNORE
(via existence check) so re-running the migration after manual seeding
doesn't fail.

This follows the pattern documented in migration ``039_analytics_view_perm``.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "047_subscriptions_view_perm"
down_revision = "046_users_view_perm"
branch_labels = None
depends_on = None


PERMISSION_KEY = "subscriptions.view"


def upgrade() -> None:
    bind = op.get_bind()

    row = bind.execute(
        sa.text("SELECT id FROM roles WHERE slug = :slug"),
        {"slug": "superadmin"},
    ).first()
    if row is None:
        return
    role_id = row[0]

    exists = bind.execute(
        sa.text(
            "SELECT 1 FROM role_permissions "
            "WHERE role_id = :role_id AND permission_key = :key"
        ),
        {"role_id": role_id, "key": PERMISSION_KEY},
    ).first()
    if exists is not None:
        return

    bind.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_key) "
            "VALUES (:role_id, :key)"
        ),
        {"role_id": role_id, "key": PERMISSION_KEY},
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_key = :key"
        ),
        {"key": PERMISSION_KEY},
    )
