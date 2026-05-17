"""Seed users.delete permission into the superadmin role.

Revision ID: 048_users_delete_perm
Revises: 047_subscriptions_view_perm
Create Date: 2026-05-17

Adds the ``users.delete`` permission key (registered in
``app/auth/permissions.py``'s ``Permission`` literal + ``ALL_PERMISSIONS``)
to the superadmin role. Gates the new system-level hard-delete endpoint
at ``DELETE /api/v1/admin/users/{user_id}``.

The runtime resolver short-circuits via ``is_superadmin``, so the seed
below is for parity with future non-superadmin roles (none should
inherit ``users.delete`` by default) and to keep the ``/admin/roles``
UI's permission editor accurate. We INSERT-IGNORE (via existence check)
so re-running the migration after manual seeding doesn't fail.

This follows the pattern documented in migration
``033_add_roles_and_role_permissions`` and the preceding
``046_seed_users_view_permission`` / ``047_seed_subscriptions_view_permission``.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "048_users_delete_perm"
down_revision = "047_subscriptions_view_perm"
branch_labels = None
depends_on = None


PERMISSION_KEY = "users.delete"


def upgrade() -> None:
    bind = op.get_bind()

    # Locate the superadmin role; if the row is missing (e.g. someone
    # ran the downgrade for 033 manually), bail silently. The runtime
    # short-circuit still covers superadmins, and this seed is purely
    # for UI parity.
    row = bind.execute(
        sa.text("SELECT id FROM roles WHERE slug = :slug"),
        {"slug": "superadmin"},
    ).first()
    if row is None:
        return
    role_id = row[0]

    # Idempotent insert: skip if the (role_id, permission_key) row
    # already exists. Composite PK means a plain INSERT would explode
    # on rerun under MySQL.
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
