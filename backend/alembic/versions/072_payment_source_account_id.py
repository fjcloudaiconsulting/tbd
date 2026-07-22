"""Add payment_source_account_id to accounts (Payment Source Foundation).

Revision ID: 072_payment_source_account_id
Revises: 071_api_tokens
Create Date: 2026-07-22

Foundation slice for the liability-payment-source feature
(``specs/payment-source-account-foundation.md``). Ships the shared
plumbing BEFORE any Credit Card or Loan UX so those slices can consume
the field without coupling to each other in review.

Schema placement follows the V1A decision
(``specs/payment-source-schema-decision-memo.md``): the field lives
directly on ``accounts``, matching the shipped fat-account-row idiom
(``close_day`` / ``payment_day`` / ``payment_day_relative_month`` are
already nullable credit-card-only columns on ``accounts``).

One additive column on ``accounts``:

    payment_source_account_id INT NULL
      FK -> accounts(id) ON DELETE SET NULL

Self-referential: the "account this liability's bill is paid FROM". All
existing rows start NULL — no backfill. ``ON DELETE SET NULL`` means
deleting the source account clears the pointer automatically rather than
blocking the delete or orphaning a dangling id. MySQL 8 auto-creates the
covering index the FK constraint requires (errno 1553 class, see
``reference_mysql_fk_index_cover``), so no explicit ``create_index``.

No payment automation, no generated transactions, no cron jobs — this
migration only lands the column + constraint.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "072_payment_source_account_id"
down_revision: Union[str, None] = "071_api_tokens"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("payment_source_account_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_accounts_payment_source_account_id",
        "accounts",
        "accounts",
        ["payment_source_account_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_accounts_payment_source_account_id", "accounts", type_="foreignkey"
    )
    op.drop_column("accounts", "payment_source_account_id")
