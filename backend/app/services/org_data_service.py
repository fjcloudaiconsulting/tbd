"""Tenant-scoped org data service (L3.1).

Owns the FK-safe wipe-order knowledge for org-scoped data tables.
``wipe_org_data`` is intentionally public — admin_orgs_service imports
it for the cascade delete path. Putting it here (neutral location)
keeps tenant code from depending on an admin service.
"""
from __future__ import annotations

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountType
from app.models.billing import BillingPeriod
from app.models.budget import Budget
from app.models.category import Category
from app.models.category_rule import CategoryRule
from app.models.forecast_plan import ForecastPlan, ForecastPlanItem
from app.models.recurring import RecurringTransaction
from app.models.transaction import Transaction


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


async def reset_org_data(
    db: AsyncSession, *, org_id: int
) -> dict[str, int]:
    """Reset all financial / import / setup data for ``org_id``.

    Tenant-scoped wrapper over :func:`wipe_org_data`. Kept distinct
    so future reset-side concerns (post-reset hooks, audit-table
    writes once L4.7 lands) have a place to live without bleeding
    into the helper. Caller commits.
    """
    return await wipe_org_data(db, org_id=org_id)
