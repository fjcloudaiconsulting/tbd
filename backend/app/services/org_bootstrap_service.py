"""Idempotent seeding of system defaults for an org.

Single source of truth for the post-registration "starter state":
system account types, system master + child categories, and the
shared Transfer system category. Used by:

- ``auth.register`` (initial seed when a new org is created)
- ``org_data_service.reset_org_data`` (re-seed after a self-service
  reset, so the org returns to the post-registration state instead
  of an empty shell)

Called from inside an active session; flushes between rows so child
inserts can reference parent IDs but does not commit. Caller controls
the transaction boundary.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import AccountType, SYSTEM_ACCOUNT_TYPES
from app.models.category import Category, CategoryType, SYSTEM_CATEGORIES


# Standalone system categories (no children, `type=both`) seeded
# alongside the SYSTEM_CATEGORIES master-and-child tree. Defined once
# here so ``seed_org_defaults`` (registration / reset) and
# ``restore_recommended_categories`` (Layer C of the Category Fallback
# design — re-runs the seed against an existing org) cannot drift.
# Architect feedback on PR #297: before this list existed, ``restore``
# only seeded ``Transfer`` even though ``seed_org_defaults`` had
# started seeding ``Credit Card Payment`` via PR #296, leaving two
# different definitions of "recommended categories" on main.
STANDALONE_SYSTEM_CATEGORIES: list[dict] = [
    {
        "slug": "transfer",
        "name": "Transfer",
        "description": "Internal transfers between accounts",
        "type": CategoryType.BOTH,
    },
    {
        # Paying a credit card bill is a transfer between a payment
        # account and a credit-card account, NOT an expense. The
        # expense-only "Debt Repayment / Credit Cards" subcategory
        # (under Debt Repayment in SYSTEM_CATEGORIES) is intentionally
        # kept distinct — it tracks the COST of carrying debt
        # (interest, fees), which is a real expense, not a transfer.
        # PR #296 added this seed for new/reset orgs; PR #297 brings
        # the same row to existing orgs via the Restore action.
        "slug": "credit_card_payment",
        "name": "Credit Card Payment",
        "description": "Payments toward a credit-card balance (transfer)",
        "type": CategoryType.BOTH,
    },
]


async def seed_org_defaults(db: AsyncSession, *, org_id: int) -> dict[str, int]:
    """Insert system account types and categories for ``org_id``.

    Idempotent: existing rows with matching ``(org_id, slug, is_system=True)``
    are left in place; only missing rows are inserted. Safe to call at
    registration AND at reset (after a wipe).

    Returns a dict with counts of newly inserted rows per table:
    ``{"account_types": N, "categories": M}``. Caller commits.
    """
    counts = {"account_types": 0, "categories": 0}

    # ── Account types ──────────────────────────────────────────────
    existing_at_slugs = set(
        (await db.scalars(
            select(AccountType.slug).where(
                AccountType.org_id == org_id,
                AccountType.is_system.is_(True),
            )
        )).all()
    )
    for sat in SYSTEM_ACCOUNT_TYPES:
        if sat["slug"] not in existing_at_slugs:
            db.add(AccountType(
                org_id=org_id,
                name=sat["name"],
                slug=sat["slug"],
                is_system=True,
            ))
            counts["account_types"] += 1

    # ── Categories (master + children + Transfer) ─────────────────
    existing_cat_slugs = set(
        (await db.scalars(
            select(Category.slug).where(
                Category.org_id == org_id,
                Category.is_system.is_(True),
            )
        )).all()
    )

    for master_def in SYSTEM_CATEGORIES:
        master: Category | None
        if master_def["slug"] in existing_cat_slugs:
            # Master already present — fetch it so children can attach.
            master = await db.scalar(
                select(Category).where(
                    Category.org_id == org_id,
                    Category.slug == master_def["slug"],
                    Category.is_system.is_(True),
                )
            )
        else:
            master = Category(
                org_id=org_id,
                name=master_def["name"],
                slug=master_def["slug"],
                description=master_def["description"],
                type=CategoryType(master_def["type"]),
                is_system=True,
            )
            db.add(master)
            counts["categories"] += 1
            # Flush so master.id is populated for the children below.
            await db.flush()

        for child_def in master_def.get("children", []):
            if child_def["slug"] in existing_cat_slugs:
                continue
            db.add(Category(
                org_id=org_id,
                parent_id=master.id if master is not None else None,
                name=child_def["name"],
                slug=child_def["slug"],
                description=child_def["description"],
                type=CategoryType(master_def["type"]),
                is_system=True,
            ))
            counts["categories"] += 1

    # Standalone system categories (Transfer, Credit Card Payment).
    # Defined once at module level so ``restore_recommended_categories``
    # cannot drift from this seed.
    for spec in STANDALONE_SYSTEM_CATEGORIES:
        if spec["slug"] not in existing_cat_slugs:
            db.add(Category(
                org_id=org_id,
                name=spec["name"],
                slug=spec["slug"],
                description=spec["description"],
                type=spec["type"],
                is_system=True,
            ))
            counts["categories"] += 1

    await db.flush()
    return counts


async def restore_recommended_categories(
    db: AsyncSession, *, org_id: int
) -> int:
    """Re-run the system-categories seed for ``org_id``. Idempotent.

    Skips any ``(org_id, slug)`` already present with ``is_system=True``.
    Existing categories (system or user-created) are never modified or
    removed. Returns the number of newly inserted Category rows so the
    UI can render an accurate "Restored N categories" toast.

    Caller (router) owns the transaction boundary and the audit row.
    Category Fallback design Layer C (post-L3.10).
    """
    created = 0

    existing_slugs = set(
        (await db.scalars(
            select(Category.slug).where(
                Category.org_id == org_id,
                Category.is_system.is_(True),
            )
        )).all()
    )

    for master_def in SYSTEM_CATEGORIES:
        master: Category | None
        if master_def["slug"] in existing_slugs:
            master = await db.scalar(
                select(Category).where(
                    Category.org_id == org_id,
                    Category.slug == master_def["slug"],
                    Category.is_system.is_(True),
                )
            )
        else:
            master = Category(
                org_id=org_id,
                name=master_def["name"],
                slug=master_def["slug"],
                description=master_def["description"],
                type=CategoryType(master_def["type"]),
                is_system=True,
            )
            db.add(master)
            created += 1
            await db.flush()

        for child_def in master_def.get("children", []):
            if child_def["slug"] in existing_slugs:
                continue
            db.add(Category(
                org_id=org_id,
                parent_id=master.id if master is not None else None,
                name=child_def["name"],
                slug=child_def["slug"],
                description=child_def["description"],
                type=CategoryType(master_def["type"]),
                is_system=True,
            ))
            created += 1

    # Standalone system categories (Transfer, Credit Card Payment).
    # Source list is ``STANDALONE_SYSTEM_CATEGORIES`` at module level so
    # this stays in lockstep with ``seed_org_defaults``. Architect
    # feedback on PR #297: previous version restored only Transfer and
    # missed Credit Card Payment (which #296 added to seed_org_defaults
    # before this PR landed), creating two different definitions of
    # "recommended categories".
    for spec in STANDALONE_SYSTEM_CATEGORIES:
        if spec["slug"] not in existing_slugs:
            db.add(Category(
                org_id=org_id,
                name=spec["name"],
                slug=spec["slug"],
                description=spec["description"],
                type=spec["type"],
                is_system=True,
            ))
            created += 1

    await db.flush()
    return created
