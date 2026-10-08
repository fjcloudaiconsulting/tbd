"""Converge prod and fresh schemas on four columns and one index (INFRA-129).

Revision ID: 087_schema_convergence
Revises: 086_platform_ai
Create Date: 2026-10-08

Prod was built while migrations 030/033 still said ``CURRENT_TIMESTAMP(6)``
and 050 emitted ``(now(6))``; a fresh ``upgrade head`` gives ``(now())`` on
030/033. Prod also kept a redundant ``KEY org_id`` on ``categories`` that
``uq_categories_org_slug_system`` (leading ``org_id``) already covers for the
FK. Canonical is the fresh side, matching every other timestamp default:

- ``audit_events.created_at``, ``roles.created_at``, ``roles.updated_at``,
  ``notifications.created_at``: ``DEFAULT (now())``. The columns are DATETIME
  (fsp 0), so ``now(6)`` only rounded instead of truncating.
- ``categories``: no ``org_id`` index.

Converges from either starting state (prod, or a fresh DB such as staging).
MySQL only, metadata only: SET DEFAULT is ALGORITHM=INSTANT and DROP INDEX is
ALGORITHM=INPLACE LOCK=NONE; both are pinned so MySQL errors instead of
silently rebuilding a table. No data change. ``downgrade`` restores prod's
shape. ``feedback_entries`` index order differs only cosmetically and is left.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "087_schema_convergence"
down_revision: Union[str, None] = "086_platform_ai"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = {
    "audit_events": ("created_at",),
    "roles": ("created_at", "updated_at"),
    "notifications": ("created_at",),
}


def _set_defaults(expr: str) -> None:
    for table, cols in COLUMNS.items():
        # Explicit parentheses: a bare ``now()`` is stored as CURRENT_TIMESTAMP.
        alters = ", ".join(f"ALTER COLUMN {c} SET DEFAULT ({expr})" for c in cols)
        op.execute(f"ALTER TABLE {table} {alters}, ALGORITHM=INSTANT")


def _has_org_id_index() -> bool:
    return op.get_bind().execute(sa.text(
        "SELECT 1 FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_NAME = 'categories' AND INDEX_NAME = 'org_id' LIMIT 1"
    )).first() is not None


def upgrade() -> None:
    if op.get_bind().dialect.name != "mysql":
        return
    _set_defaults("now()")
    if _has_org_id_index():
        op.execute("ALTER TABLE categories DROP INDEX org_id, ALGORITHM=INPLACE, LOCK=NONE")


def downgrade() -> None:
    if op.get_bind().dialect.name != "mysql":
        return
    _set_defaults("now(6)")
    if not _has_org_id_index():
        op.execute("ALTER TABLE categories ADD INDEX org_id (org_id), ALGORITHM=INPLACE, LOCK=NONE")
