"""Case-insensitive UNIQUE on organizations.name (Track D org-rename).

Revision ID: 034_unique_org_name
Revises: 033_roles
Create Date: 2026-05-07

Backstory:
    Track D introduces an OWNER-only "rename my org" endpoint
    (PATCH /api/v1/orgs/{org_id}/rename). Two orgs with the same
    canonical name are confusing for cross-tenant features (admin
    surfaces, audit logs, support tickets), so the architect locked
    in a DB-level uniqueness guarantee on the name's lower-cased,
    accent-sensitive form. The router does a friendly preflight 409;
    this constraint is the backstop that survives any race.

MySQL strategy:
    Add a STORED generated column ``name_normalized`` that mirrors
    ``LOWER(name)`` with collation ``utf8mb4_0900_as_cs``. The
    generated column is what carries the UNIQUE constraint. Accent-
    sensitive collation distinguishes "Cafe" from "Café" (the spec's
    decision: no surprising deduplication of distinct trade names).

SQLite strategy:
    SQLite supports virtual generated columns but stored generated
    columns can be platform-flaky on older SQLite builds, and the
    unit-test fixtures use ``aiosqlite`` so we keep it simple: a
    plain UNIQUE INDEX on ``LOWER(name)``. SQLite's LOWER() is
    binary on non-ASCII, which actually mirrors the MySQL accent-
    sensitive behaviour (accents are preserved, only ASCII case is
    folded), so the same dedupe semantics hold for tests.

Defensive precheck:
    Both dialects scan for any existing duplicate (case-insensitive)
    org names before applying the constraint. Pre-launch the only
    seeded data is the local dev DB, but the migration is cheap to
    keep idempotent, and a future re-run on a clobbered dataset
    would bail with a useful error rather than crashing on the DDL.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "034_unique_org_name"
down_revision = "033_roles"
branch_labels = None
depends_on = None


def _check_no_duplicates(bind) -> None:
    """Bail with a clear error if duplicate (case-insensitive) org
    names already exist. Pre-launch insurance: the DDL would fail
    anyway, but this surfaces the offending rows up front so an
    operator can fix them in one pass.
    """
    rows = bind.execute(sa.text(
        "SELECT LOWER(name) AS norm, COUNT(*) AS n "
        "FROM organizations "
        "GROUP BY LOWER(name) "
        "HAVING n > 1"
    )).all()
    if rows:
        offenders = ", ".join(f"{r.norm!r} (x{r.n})" for r in rows)
        raise RuntimeError(
            "Cannot apply UNIQUE on organizations.name (case-insensitive): "
            f"duplicates already exist: {offenders}. "
            "Resolve the duplicates and re-run the migration."
        )


def upgrade() -> None:
    bind = op.get_bind()
    _check_no_duplicates(bind)

    if bind.dialect.name == "mysql":
        # Generated column carries the constraint. The expression
        # LOWER(name) is deterministic and the column is STORED so
        # the index is point-lookup cheap.
        op.execute(
            "ALTER TABLE organizations "
            "ADD COLUMN name_normalized VARCHAR(200) "
            "GENERATED ALWAYS AS (LOWER(name)) STORED "
            "COLLATE utf8mb4_0900_as_cs"
        )
        op.create_unique_constraint(
            "uq_organizations_name_normalized",
            "organizations",
            ["name_normalized"],
        )
    else:
        # SQLite (test) and any other dialect: plain UNIQUE INDEX on
        # the expression. SQLite supports indexed expressions since
        # 3.9.0 (2015) — well below our floor.
        op.execute(
            "CREATE UNIQUE INDEX uq_organizations_name_normalized "
            "ON organizations (LOWER(name))"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.drop_constraint(
            "uq_organizations_name_normalized",
            "organizations",
            type_="unique",
        )
        op.drop_column("organizations", "name_normalized")
    else:
        op.drop_index(
            "uq_organizations_name_normalized",
            table_name="organizations",
        )
