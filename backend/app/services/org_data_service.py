"""Tenant-scoped org data service (L3.1).

Owns the FK-safe wipe-order knowledge for org-scoped data tables.
``wipe_org_data`` is intentionally public — admin_orgs_service imports
it for the cascade delete path. Putting it here (neutral location)
keeps tenant code from depending on an admin service.

Two distinct paths:

- ``wipe_org_data`` (admin delete) issues unbounded ``DELETE WHERE
  org_id = :id`` statements inside the caller's transaction. The
  whole org is going away, so partial-state risk is moot and the
  caller wants one commit boundary.
- ``reset_org_data`` (self-service tenant reset) issues batched
  ``DELETE WHERE id IN (...)`` over PK chunks with a commit between
  each chunk. Releases locks so other traffic can interleave on a
  single-replica MySQL instance. Accepts partial-wipe risk on
  interruption — the operation is idempotent (re-running picks up
  any remaining rows + re-runs the seed).
"""
from __future__ import annotations

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountType
from app.models.billing import BillingPeriod
from app.models.budget import Budget
from app.models.category import Category
from app.models.category_rule import CategoryRule
from app.models.forecast_plan import ForecastPlan, ForecastPlanItem
from app.models.recurring import RecurringTransaction
from app.models.transaction import Transaction
from app.services.org_bootstrap_service import seed_org_defaults


async def wipe_org_data(
    db: AsyncSession, *, org_id: int
) -> dict[str, int]:
    """Delete every row in org-scoped data tables for ``org_id``.

    Preserves the org shell (organizations, users, subscriptions,
    org_settings, org_feature_overrides, invitations). Never touches
    cross-org tables (e.g. merchant_dictionary). Caller commits.

    Returns a dict of ``{table: rowcount}``. Single source of truth
    for the wipe-order across both this service's reset path AND
    ``admin_orgs_service.delete_org_cascade``.
    """
    counts: dict[str, int] = {}

    # Order matters: delete children before parents.
    counts["transactions"] = (
        await db.execute(delete(Transaction).where(Transaction.org_id == org_id))
    ).rowcount or 0

    counts["forecast_plan_items"] = (
        await db.execute(
            delete(ForecastPlanItem).where(ForecastPlanItem.org_id == org_id)
        )
    ).rowcount or 0

    counts["budgets"] = (
        await db.execute(delete(Budget).where(Budget.org_id == org_id))
    ).rowcount or 0

    counts["recurring_transactions"] = (
        await db.execute(
            delete(RecurringTransaction).where(RecurringTransaction.org_id == org_id)
        )
    ).rowcount or 0

    counts["forecast_plans"] = (
        await db.execute(delete(ForecastPlan).where(ForecastPlan.org_id == org_id))
    ).rowcount or 0

    counts["billing_periods"] = (
        await db.execute(delete(BillingPeriod).where(BillingPeriod.org_id == org_id))
    ).rowcount or 0

    counts["accounts"] = (
        await db.execute(delete(Account).where(Account.org_id == org_id))
    ).rowcount or 0

    counts["account_types"] = (
        await db.execute(delete(AccountType).where(AccountType.org_id == org_id))
    ).rowcount or 0

    # category_rules.category_id FKs to categories.id, so it must be
    # deleted before the bulk DELETE on categories.
    counts["category_rules"] = (
        await db.execute(delete(CategoryRule).where(CategoryRule.org_id == org_id))
    ).rowcount or 0

    # Categories self-reference via parent_id. Break the link before
    # the bulk DELETE so MySQL's strict FK doesn't refuse.
    await db.execute(
        update(Category).where(Category.org_id == org_id).values(parent_id=None)
    )
    counts["categories"] = (
        await db.execute(delete(Category).where(Category.org_id == org_id))
    ).rowcount or 0

    return counts


# Default chunk size for batched reset deletes. 500 rows per batch
# is a balance between (a) keeping each transaction's lock window
# short enough to not wedge a single-replica MySQL under load, and
# (b) not bloating the round-trip count for typical household
# volumes (a real customer org has dozens of accounts, hundreds to
# low-thousands of transactions). Tunable via the ``batch_size``
# kwarg on ``reset_org_data`` if real workloads warrant.
RESET_BATCH_SIZE = 500


async def _batch_delete_by_pk(
    db: AsyncSession,
    model: type,
    org_id: int,
    label: str,
    batch_size: int,
) -> int:
    """Delete rows from ``model`` matching ``org_id`` in PK-id chunks.

    Selects PKs first (cheap, indexed on ``id`` + ``org_id``), deletes
    by ``WHERE id IN (...)``, commits, repeats. Each commit releases
    the lock window so concurrent traffic can interleave. The select
    finds a fresh batch each iteration (already-deleted rows fall out
    of the result set), so no offset bookkeeping is needed.

    The ``label`` argument is for caller logging only; this function
    just returns the total deleted count.
    """
    total = 0
    while True:
        ids = list((await db.scalars(
            select(model.id).where(model.org_id == org_id).limit(batch_size)
        )).all())
        if not ids:
            break
        result = await db.execute(
            delete(model).where(model.id.in_(ids))
        )
        total += result.rowcount or 0
        await db.commit()
        if len(ids) < batch_size:
            break
    return total


async def reset_org_data(
    db: AsyncSession, *, org_id: int, batch_size: int = RESET_BATCH_SIZE
) -> dict[str, int]:
    """Reset all financial / import / setup data for ``org_id`` and
    re-seed system defaults.

    Distinct from :func:`wipe_org_data` (admin delete path):

    - Deletes are batched by PK with a ``db.commit()`` between
      chunks so locks release and other traffic can interleave
      on the single-replica DO instance.
    - After the wipe completes, calls
      :func:`org_bootstrap_service.seed_org_defaults` to restore the
      post-registration state: system account types, system master +
      child categories, and the Transfer category.

    Returns a dict of ``{table: rowcount}`` for the wipe plus
    ``seeded_account_types`` and ``seeded_categories`` counts.

    Caller does NOT commit afterward — this function manages its own
    transaction boundaries (per-batch + a final commit on the seed).
    Endpoint should rollback only if an exception escapes; committed
    batches up to that point persist, and the user can re-run the
    reset to finish (idempotent).
    """
    counts: dict[str, int] = {}

    counts["transactions"] = await _batch_delete_by_pk(
        db, Transaction, org_id, "transactions", batch_size
    )
    counts["forecast_plan_items"] = await _batch_delete_by_pk(
        db, ForecastPlanItem, org_id, "forecast_plan_items", batch_size
    )
    counts["budgets"] = await _batch_delete_by_pk(
        db, Budget, org_id, "budgets", batch_size
    )
    counts["recurring_transactions"] = await _batch_delete_by_pk(
        db, RecurringTransaction, org_id, "recurring_transactions", batch_size
    )
    counts["forecast_plans"] = await _batch_delete_by_pk(
        db, ForecastPlan, org_id, "forecast_plans", batch_size
    )
    counts["billing_periods"] = await _batch_delete_by_pk(
        db, BillingPeriod, org_id, "billing_periods", batch_size
    )
    counts["accounts"] = await _batch_delete_by_pk(
        db, Account, org_id, "accounts", batch_size
    )
    counts["account_types"] = await _batch_delete_by_pk(
        db, AccountType, org_id, "account_types", batch_size
    )
    counts["category_rules"] = await _batch_delete_by_pk(
        db, CategoryRule, org_id, "category_rules", batch_size
    )

    # Categories self-reference via parent_id. Break the link as a
    # single UPDATE before the batched delete so MySQL's strict FK
    # check does not refuse mid-chunk. Children are typically a small
    # set vs the whole categories table, so the UPDATE is cheap and
    # does not warrant batching itself.
    await db.execute(
        update(Category).where(Category.org_id == org_id).values(parent_id=None)
    )
    await db.commit()
    counts["categories"] = await _batch_delete_by_pk(
        db, Category, org_id, "categories", batch_size
    )

    # Re-seed the post-registration defaults. Idempotent: if the
    # caller is retrying after a partial wipe, existing defaults are
    # left in place. A single commit at the end caps the seed so
    # the per-batch wipe + seed all reach a consistent state.
    seeded = await seed_org_defaults(db, org_id=org_id)
    counts["seeded_account_types"] = seeded["account_types"]
    counts["seeded_categories"] = seeded["categories"]
    await db.commit()

    return counts
