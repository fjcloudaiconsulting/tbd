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
from app.models.cc_cycle_payment import CcCyclePayment
from app.models.forecast_plan import ForecastPlan, ForecastPlanItem
from app.models.import_batch import ImportBatch
from app.models.recurring import RecurringTransaction
from app.models.tag import Tag, TagDictionary, TagDictionaryContributor, TransactionTag
from app.models.transaction import Transaction
from app.services.org_bootstrap_service import seed_org_defaults


async def _decrement_dictionary_counts_for_org(
    db: AsyncSession, *, org_id: int
) -> int:
    """Decrement ``tag_dictionary.contributor_org_count`` for every
    dictionary tag this org has contributed to, then delete the
    contributor rows.

    This keeps the k-anonymity invariant
    (``contributor_org_count == COUNT(DISTINCT contributor_org_id)``)
    intact when an org goes away. Without it, below-floor tags can
    still surface as suggestions even though the org no longer exists.

    Order:
        1. SELECT every ``dictionary_tag_id`` this org contributed to.
        2. UPDATE ``tag_dictionary`` to ``contributor_org_count - 1``
           for each one (one statement per tag id is fine, the count
           is per-org so the worst case is the number of distinct
           dictionary tags this org touched, which is bounded by the
           org's tag count).
        3. DELETE the contributor rows for this org.

    Returns the number of contributor rows that were deleted.

    Notes:
    - The contributor FK on ``contributor_org_id`` already has
      ``ON DELETE CASCADE`` so the rows would disappear when the
      org is deleted, but the count would NOT be decremented
      automatically. This function provides the explicit sync.
    - ``GREATEST(count - 1, 0)`` would be safer against count drift
      from any historical bug, but we deliberately surface the
      drift here (subtract straight) so a regression test would
      catch it. Counts are non-negative by invariant.
    """
    contributor_ids = (
        await db.execute(
            select(TagDictionaryContributor.dictionary_tag_id).where(
                TagDictionaryContributor.contributor_org_id == org_id
            )
        )
    ).scalars().all()
    for dict_tag_id in contributor_ids:
        await db.execute(
            update(TagDictionary)
            .where(TagDictionary.id == dict_tag_id)
            .values(
                contributor_org_count=TagDictionary.contributor_org_count - 1
            )
        )
    deleted = (
        await db.execute(
            delete(TagDictionaryContributor).where(
                TagDictionaryContributor.contributor_org_id == org_id
            )
        )
    ).rowcount or 0
    return deleted


async def wipe_org_data(
    db: AsyncSession, *, org_id: int
) -> dict[str, int]:
    """Delete every row in org-scoped data tables for ``org_id``.

    Preserves the org shell (organizations, users, subscriptions,
    org_settings, org_feature_overrides, invitations). Never touches
    cross-org PUBLIC tables (e.g. merchant_dictionary, tag_dictionary)
    other than to decrement the per-tag contributor count so the
    k-anonymity invariant survives org deletion. Caller commits.

    Returns a dict of ``{table: rowcount}``. Single source of truth
    for the wipe-order across both this service's reset path AND
    ``admin_orgs_service.delete_org_cascade``.

    Convention: every new org-scoped data table goes through this
    function. See ``project_roadmap.md`` TECHNICAL DEBT section.
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

    # import_batches.account_id FKs to accounts.id with no ON DELETE
    # CASCADE. Must be wiped before accounts to avoid IntegrityError 1451.
    counts["import_batches"] = (
        await db.execute(delete(ImportBatch).where(ImportBatch.org_id == org_id))
    ).rowcount or 0

    # cc_cycle_payments.account_id FKs accounts.id ON DELETE CASCADE, but
    # codebase convention deletes cascade children explicitly (accurate
    # counts + dialect-independent SQLite tests). No org_id column, so scope
    # by the org's account ids. Credit Card Model V1, Slice 2.
    counts["cc_cycle_payments"] = (
        await db.execute(
            delete(CcCyclePayment).where(
                CcCyclePayment.account_id.in_(
                    select(Account.id).where(Account.org_id == org_id)
                )
            )
        )
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

    # Tags + transaction_tags + tag_dictionary_contributors.
    # transaction_tags has ON DELETE CASCADE on both FKs, so deleting
    # transactions above already wiped the join rows; the explicit
    # DELETE here is defense in depth and a non-zero rowcount on a
    # row that survived (e.g. an orphan from an earlier bug).
    counts["transaction_tags"] = (
        await db.execute(
            delete(TransactionTag).where(
                TransactionTag.tag_id.in_(
                    select(Tag.id).where(Tag.org_id == org_id)
                )
            )
        )
    ).rowcount or 0

    # Decrement tag_dictionary.contributor_org_count for every entry
    # this org contributed to, then delete the contributor rows. Keeps
    # the k-anonymity invariant intact when the org goes away.
    counts["tag_dictionary_contributors"] = (
        await _decrement_dictionary_counts_for_org(db, org_id=org_id)
    )

    counts["tags"] = (
        await db.execute(delete(Tag).where(Tag.org_id == org_id))
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
    # import_batches.account_id FKs to accounts.id with no ON DELETE
    # CASCADE. Must be wiped before accounts to avoid IntegrityError 1451.
    counts["import_batches"] = await _batch_delete_by_pk(
        db, ImportBatch, org_id, "import_batches", batch_size
    )
    # cc_cycle_payments has no org_id column, so _batch_delete_by_pk cannot
    # scope it. Use a single subquery-scoped delete before the accounts wipe.
    # Credit Card Model V1, Slice 2.
    counts["cc_cycle_payments"] = (
        await db.execute(
            delete(CcCyclePayment).where(
                CcCyclePayment.account_id.in_(
                    select(Account.id).where(Account.org_id == org_id)
                )
            )
        )
    ).rowcount or 0

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

    # Tags: explicit join wipe (CASCADE on transaction delete already
    # cleared transaction_tags rows tied to deleted transactions, but
    # an explicit pass guarantees no orphan join rows survive). Then
    # decrement contributor counts and delete contributor rows so the
    # k-anonymity invariant on tag_dictionary stays accurate after the
    # reset.
    counts["transaction_tags"] = (
        await db.execute(
            delete(TransactionTag).where(
                TransactionTag.tag_id.in_(
                    select(Tag.id).where(Tag.org_id == org_id)
                )
            )
        )
    ).rowcount or 0
    counts["tag_dictionary_contributors"] = (
        await _decrement_dictionary_counts_for_org(db, org_id=org_id)
    )
    counts["tags"] = (
        await db.execute(delete(Tag).where(Tag.org_id == org_id))
    ).rowcount or 0
    await db.commit()

    # Re-seed the post-registration defaults. Idempotent: if the
    # caller is retrying after a partial wipe, existing defaults are
    # left in place. A single commit at the end caps the seed so
    # the per-batch wipe + seed all reach a consistent state.
    seeded = await seed_org_defaults(db, org_id=org_id)
    counts["seeded_account_types"] = seeded["account_types"]
    counts["seeded_categories"] = seeded["categories"]
    await db.commit()

    return counts
