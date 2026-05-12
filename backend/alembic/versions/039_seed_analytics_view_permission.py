"""Seed analytics.view permission into the superadmin role (L4.6).

Revision ID: 039_analytics_view_perm
Revises: 038_tags_and_dictionary
Create Date: 2026-05-11

L4.6 ships the system-usage analytics surface. It is gated by a new
``analytics.view`` permission key (added to
``app/auth/permissions.py``'s ``Permission`` literal + ``ALL_PERMISSIONS``).

The runtime resolver short-circuits via ``is_superadmin``, so the seed
below is for parity with future non-superadmin roles and to keep the
``/admin/roles`` UI's permission editor accurate. We INSERT-IGNORE
(via existence check) so re-running the migration after manual seeding
doesn't fail.

This follows the pattern documented in migration ``033_add_roles_and_role_permissions``:
"If new permissions are added later, ship a follow-up data migration
that inserts the new keys into role_permissions for the superadmin row."
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "039_analytics_view_perm"
down_revision = "038_tags_and_dictionary"
branch_labels = None
depends_on = None


PERMISSION_KEY = "analytics.view"


def upgrade() -> None:
    bind = op.get_bind()

    # Locate the superadmin role; if the row is missing (e.g. someone
    # ran the downgrade for 033 manually), bail silently — the runtime
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
