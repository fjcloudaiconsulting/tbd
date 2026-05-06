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

    # Transfer system category (CategoryType.BOTH; no children).
    if "transfer" not in existing_cat_slugs:
        db.add(Category(
            org_id=org_id,
            name="Transfer",
            slug="transfer",
            description="Internal transfers between accounts",
            type=CategoryType.BOTH,
            is_system=True,
        ))
        counts["categories"] += 1

    await db.flush()
    return counts
