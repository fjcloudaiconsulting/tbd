"""Loan Account Type V1 (Slice 1): loan columns + system-type backfill.

Revision ID: 077_loan_account_type
Revises: 076_cc_statement_category
Create Date: 2026-07-24

Two changes (see specs/2026-07-24-loan-account-type-v1-design.md §3.2):

1. Five additive nullable columns on ``accounts`` (all NULL on non-loan rows,
   fat-row idiom mirroring the CC columns). All plain types -> no native-ENUM
   or collation landmine, so SQLite CI faithfully mirrors MySQL prod:

       principal_amount   Numeric(12,2) NULL  -- original principal, > 0
       interest_rate_apr  Numeric(5,2)  NULL  -- annual %, [0, 999.99]
       term_months        SmallInteger  NULL  -- [1, 480]
       origination_date   Date          NULL
       first_payment_date Date          NULL

2. A per-org backfill inserting a ``loan`` system AccountType for every org
   that lacks one. New orgs get it via ``seed_org_defaults`` (SYSTEM_ACCOUNT_TYPES
   now includes loan); reset orgs re-run that seed; this backfill covers the
   EXISTING orgs. Pattern copied from 037_categories_floor_backfill (raw SQL on
   ``op.get_bind()``; NO ``asyncio.run`` inside Alembic's loop).

   The existence guard keys on **slug only**, not ``is_system``: there is no
   UNIQUE(org_id, slug) on account_types, so a naive insert double-creates, and
   an org that hand-created a custom (is_system=False) ``loan`` type must not get
   a second one here. NOTE this diverges slightly from ``seed_org_defaults``,
   which guards on the ``is_system=True`` slug set — so a custom-loan org could
   still receive a system ``loan`` on a future reset. Pre-launch this is
   acceptable and left as-is (aligning the seed's guard would change behavior for
   every system type).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "077_loan_account_type"
down_revision: Union[str, None] = "076_cc_statement_category"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_SELECT_ORG_IDS = sa.text("SELECT id FROM organizations")
_LOAN_EXISTS = sa.text(
    "SELECT 1 FROM account_types WHERE org_id = :org_id AND slug = 'loan' LIMIT 1"
)
_INSERT_LOAN_TYPE = sa.text(
    "INSERT INTO account_types (org_id, name, slug, is_system) "
    "VALUES (:org_id, 'Loan', 'loan', :is_system)"
)
_DELETE_UNREFERENCED_LOAN_TYPES = sa.text(
    "DELETE FROM account_types "
    "WHERE slug = 'loan' AND is_system = :is_system "
    "AND id NOT IN (SELECT account_type_id FROM accounts)"
)


def backfill_loan_type(bind) -> int:
    """Insert a ``loan`` system AccountType for every org lacking one.

    Idempotent, existence-guarded on **slug only** (there is no
    UNIQUE(org_id, slug), so a naive insert double-creates; and an org that
    hand-created a custom is_system=False ``loan`` type must not get a second).
    Module-level + returns a count so it is unit-testable on SQLite without the
    Alembic op context (the columns above are plain types, SQLite-portable).
    Returns the number of rows inserted.
    """
    inserted = 0
    org_ids = [row[0] for row in bind.execute(_SELECT_ORG_IDS).all()]
    for org_id in org_ids:
        if bind.execute(_LOAN_EXISTS, {"org_id": org_id}).first() is not None:
            continue
        bind.execute(_INSERT_LOAN_TYPE, {"org_id": org_id, "is_system": True})
        inserted += 1
    return inserted


def delete_unreferenced_loan_types(bind) -> None:
    """Delete only SYSTEM ``loan`` types with no referencing accounts, so a
    downgrade never orphans an accounts.account_type_id FK (custom is_system
    =False loan types are left untouched)."""
    bind.execute(_DELETE_UNREFERENCED_LOAN_TYPES, {"is_system": True})


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("principal_amount", sa.Numeric(12, 2), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column("interest_rate_apr", sa.Numeric(5, 2), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column("term_months", sa.SmallInteger(), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column("origination_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column("first_payment_date", sa.Date(), nullable=True),
    )

    # Per-org backfill of the loan system account type (existing orgs only).
    backfill_loan_type(op.get_bind())


def downgrade() -> None:
    delete_unreferenced_loan_types(op.get_bind())

    op.drop_column("accounts", "first_payment_date")
    op.drop_column("accounts", "origination_date")
    op.drop_column("accounts", "term_months")
    op.drop_column("accounts", "interest_rate_apr")
    op.drop_column("accounts", "principal_amount")
