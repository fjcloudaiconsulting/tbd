"""Add roles and role_permissions tables (L4.8 role admin UI).

Revision ID: 033_roles
Revises: 032_drop_legacy_plan_ai
Create Date: 2026-05-07

Persists platform-level roles (today only ``superadmin``; tomorrow:
``support``, ``operator``, ``revenue``, ``analyst``, ...) and the
permission keys each role grants. The resolver in
``app/auth/permissions.py`` keeps the ``is_superadmin`` short-circuit;
this table exists so future roles become configuration rather than a
code change to ``ROLE_PERMISSIONS``.

Schema notes:

- ``roles.id`` uses ``BigInteger`` with the ``Integer`` SQLite variant
  (same pattern as ``audit_events.id`` in 030). Roles will never grow
  to billions, but matching the project pattern is cheap.
- ``is_system_frozen`` is the UI's only guard against editing the
  ``superadmin`` row. The router enforces it again as defense in
  depth — never trust a single layer.
- ``role_permissions`` uses a composite PK on ``(role_id,
  permission_key)`` so duplicate inserts no-op via DB-level unique
  rather than service-level checks.

Seed:

- One ``superadmin`` row with ``is_system_frozen=TRUE``.
- One ``role_permissions`` row per key in ``ALL_PERMISSIONS`` at
  migration time. The list is **snapshotted** here (not imported from
  ``app.auth.permissions``) so future renames in the Permission Literal
  cannot mutate the historical migration. When ``ALL_PERMISSIONS``
  changes, follow up with a data migration; do not edit this one.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "033_roles"
down_revision = "032_drop_legacy_plan_ai"
branch_labels = None
depends_on = None


# Snapshot of ``app.auth.permissions.ALL_PERMISSIONS`` at the time this
# migration was written. ``roles.manage`` is added in this PR. If new
# permissions are added later, ship a follow-up data migration that
# inserts the new keys into ``role_permissions`` for the superadmin
# row — do NOT edit this list. The runtime resolver still
# short-circuits via ``is_superadmin``, so missing rows here never
# block superadmins; the seed is for parity with future non-superadmin
# roles.
_PERMISSION_KEYS_AT_MIGRATION_TIME = (
    "admin.view",
    "plans.manage",
    "orgs.view",
    "orgs.manage",
    "audit.view",
    "roles.manage",
)


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column(
            "is_system_frozen",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=False),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("slug", name="uq_roles_slug"),
    )

    op.create_table(
        "role_permissions",
        sa.Column(
            "role_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("permission_key", sa.String(80), nullable=False),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["roles.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("role_id", "permission_key"),
    )

    # Seed the superadmin role with every permission known at migration
    # time. The runtime resolver still short-circuits via is_superadmin
    # — this seed is so the row exists in the UI as a frozen reference
    # for what a fully-empowered role looks like.
    bind = op.get_bind()
    res = bind.execute(
        sa.text(
            """
            INSERT INTO roles (slug, name, description, is_system_frozen)
            VALUES (:slug, :name, :description, :frozen)
            """
        ),
        {
            "slug": "superadmin",
            "name": "Superadmin",
            "description": (
                "Full platform access. Frozen system role; cannot be "
                "edited or deleted."
            ),
            "frozen": True,
        },
    )

    # Resolve the inserted id portably (lastrowid works on MySQL,
    # SQLite, and Postgres; no need for RETURNING).
    role_id = res.lastrowid
    if role_id is None:
        # Fallback: SELECT by slug (Postgres lastrowid behaviour
        # depends on the driver).
        row = bind.execute(
            sa.text("SELECT id FROM roles WHERE slug = :slug"),
            {"slug": "superadmin"},
        ).first()
        role_id = row[0]

    bind.execute(
        sa.text(
            """
            INSERT INTO role_permissions (role_id, permission_key)
            VALUES (:role_id, :permission_key)
            """
        ),
        [
            {"role_id": role_id, "permission_key": key}
            for key in _PERMISSION_KEYS_AT_MIGRATION_TIME
        ],
    )


def downgrade() -> None:
    # drop_table on role_permissions first — its FK to roles.id would
    # otherwise block the drop on MySQL.
    op.drop_table("role_permissions")
    op.drop_table("roles")
