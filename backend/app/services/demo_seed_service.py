"""Demo-data seed (L3.3 onboarding wizard).

Generates a compact, realistic dataset for ONE org so a brand-new user
can poke around with non-empty data after the first-run wizard. The
service is intentionally smaller and faster than ``backend/seed.py``
(which is an HTTP-driven CLI populating a developer's local DB);
this version writes directly through the SQLAlchemy session and is
designed to run inside a request handler (sub-second on a fresh org).

Idempotency contract — the endpoint refuses to seed when:

1. The org already has any non-imported, non-manual-adjustment
   transaction. Real user data MUST NOT collide with the demo set.
2. The sentinel category ``DEMO_SENTINEL_SLUG`` already exists in the
   org. That category is created by ``seed_org`` itself, so its
   presence means a prior call already succeeded.

Both checks raise :class:`DemoSeedAlreadyApplied` (mapped to HTTP 409
``org_has_data`` at the router layer). The caller does NOT have to
unwind on failure — the service never half-writes (every mutation
lives in the same ``db`` session and the router rolls back on raise).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

import structlog
from dateutil.relativedelta import relativedelta
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountType
from app.models.category import Category, CategoryType
from app.models.transaction import (
    Transaction,
    TransactionStatus,
    TransactionType,
)


logger = structlog.stdlib.get_logger()


# Sentinel category slug stored on the seeded category row. We check
# for its presence to refuse a second seed against the same org; the
# slug uses the ``demo_`` prefix so the user can ignore or hide it
# from real spending reports.
DEMO_SENTINEL_SLUG = "demo_seed_sentinel"


class DemoSeedAlreadyApplied(Exception):
    """Raised when the org already has data (real or seeded)."""


@dataclass
class SeedResult:
    accounts_created: int
    transactions_created: int
    categories_created: int


async def _has_real_data(db: AsyncSession, org_id: int) -> bool:
    """True if the org has any user-created (non-imported, non-adjustment) tx."""
    stmt = select(func.count(Transaction.id)).where(
        Transaction.org_id == org_id,
        Transaction.is_manual_adjustment.is_(False),
    )
    count = await db.scalar(stmt)
    return bool(count)


async def _has_sentinel(db: AsyncSession, org_id: int) -> bool:
    stmt = select(Category.id).where(
        Category.org_id == org_id,
        Category.slug == DEMO_SENTINEL_SLUG,
    )
    return (await db.scalar(stmt)) is not None


async def _checking_account_type(db: AsyncSession, org_id: int) -> AccountType:
    """Find the org's checking account type, or fall back to any system type.

    A freshly-bootstrapped org has the system account types installed
    by ``org_bootstrap_service`` (checking / savings / credit_card /
    investment). If the org has been customized and ``checking`` is
    missing we fall back to the first system type — the demo seed
    should never crash because the user renamed an account type.
    """
    stmt = select(AccountType).where(AccountType.org_id == org_id)
    rows = (await db.execute(stmt)).scalars().all()
    for at in rows:
        if at.slug == "checking":
            return at
    for at in rows:
        if at.slug == "savings":
            return at
    if rows:
        return rows[0]
    raise DemoSeedAlreadyApplied("org has no account types — cannot seed")


async def _pick_category(
    db: AsyncSession, org_id: int, slug: str, fallback_type: CategoryType
) -> Optional[Category]:
    """Best-effort category lookup. Returns None if the org has none matching."""
    stmt = select(Category).where(
        Category.org_id == org_id, Category.slug == slug
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row:
        return row
    # Fall back to any system category with a matching type so the seed
    # never crashes against a stripped-down org. Order by id for stable
    # picks across test runs.
    stmt = (
        select(Category)
        .where(
            Category.org_id == org_id,
            Category.type.in_([fallback_type, CategoryType.BOTH]),
        )
        .order_by(Category.id.asc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def seed_org(db: AsyncSession, org_id: int) -> SeedResult:
    """Populate ``org_id`` with the L3.3 demo dataset.

    Org isolation: every INSERT carries ``org_id`` explicitly. The
    service NEVER iterates over other orgs and NEVER reads any row
    from a different org_id. Tests assert this in
    ``tests/services/test_demo_seed_service.py``.

    Caller is expected to:
      1. Await this function inside a request-scoped transaction.
      2. Commit on return.
      3. Catch :class:`DemoSeedAlreadyApplied` and map to HTTP 409.
    """
    if await _has_real_data(db, org_id):
        raise DemoSeedAlreadyApplied("org already has user data")
    if await _has_sentinel(db, org_id):
        raise DemoSeedAlreadyApplied("org already seeded with demo data")

    today = date.today()

    # 1. Sentinel category. Slug is unique by convention; the seed
    # writes it FIRST so a concurrent re-entry trips the sentinel
    # check after the flush below.
    sentinel = Category(
        org_id=org_id,
        name="(demo)",
        description="Marker category for demo-seeded transactions. Safe to delete.",
        slug=DEMO_SENTINEL_SLUG,
        is_system=False,
        type=CategoryType.BOTH,
    )
    db.add(sentinel)
    await db.flush()

    # 2. Accounts. Small set so the dashboard has signal without
    # drowning the user. Names are brand-neutral; the user can rename.
    checking_type = await _checking_account_type(db, org_id)
    accounts = []
    account_specs = [
        ("Sample Checking", Decimal("3200.00")),
        ("Sample Savings", Decimal("8500.00")),
    ]
    for name, balance in account_specs:
        acct = Account(
            org_id=org_id,
            account_type_id=checking_type.id,
            name=name,
            balance=balance,
            currency="EUR",
            is_active=True,
            is_default=(name == "Sample Checking"),
        )
        db.add(acct)
        accounts.append(acct)
    await db.flush()

    # 3. Resolve a couple of categories. If the org has the standard
    # system tree they will resolve by slug; otherwise we fall back.
    salary_cat = await _pick_category(db, org_id, "paycheck", CategoryType.INCOME)
    groceries_cat = await _pick_category(db, org_id, "groceries", CategoryType.EXPENSE)
    rent_cat = await _pick_category(db, org_id, "rent_mortgage", CategoryType.EXPENSE)
    coffee_cat = await _pick_category(db, org_id, "coffee_shops", CategoryType.EXPENSE)
    # Final fallback: the sentinel itself, which is BOTH-typed. We
    # never want to leave a transaction without a category.
    fallback = sentinel
    salary_cat = salary_cat or fallback
    groceries_cat = groceries_cat or fallback
    rent_cat = rent_cat or fallback
    coffee_cat = coffee_cat or fallback

    checking = accounts[0]
    tx_count = 0

    # 4. Transactions: two months of light history. Compact on
    # purpose so the seed is fast and the dashboard demo data does
    # not overshadow the user's real activity if they later import.
    for month_offset in (1, 0):
        m_start = today.replace(day=1) - relativedelta(months=month_offset)
        # Salary on the 25th.
        sal_day = m_start.replace(day=min(25, 28))
        if sal_day <= today:
            tx = Transaction(
                org_id=org_id,
                account_id=checking.id,
                category_id=salary_cat.id,
                description="Sample salary",
                amount=Decimal("3500.00"),
                type=TransactionType.INCOME,
                status=TransactionStatus.SETTLED,
                date=sal_day,
                settled_date=sal_day,
            )
            db.add(tx)
            tx_count += 1

        # Rent on the 1st.
        rent_day = m_start
        if rent_day <= today:
            tx = Transaction(
                org_id=org_id,
                account_id=checking.id,
                category_id=rent_cat.id,
                description="Sample rent",
                amount=Decimal("1100.00"),
                type=TransactionType.EXPENSE,
                status=TransactionStatus.SETTLED,
                date=rent_day,
                settled_date=rent_day,
            )
            db.add(tx)
            tx_count += 1

        # A handful of groceries + coffee spread across the month.
        for day_offset, amount, cat, desc in [
            (4, Decimal("62.40"), groceries_cat, "Sample groceries"),
            (11, Decimal("48.10"), groceries_cat, "Sample groceries"),
            (18, Decimal("54.95"), groceries_cat, "Sample groceries"),
            (6, Decimal("4.80"), coffee_cat, "Sample coffee"),
            (13, Decimal("5.20"), coffee_cat, "Sample coffee"),
        ]:
            tx_date = m_start + timedelta(days=day_offset)
            if tx_date > today:
                continue
            tx = Transaction(
                org_id=org_id,
                account_id=checking.id,
                category_id=cat.id,
                description=desc,
                amount=amount,
                type=TransactionType.EXPENSE,
                status=TransactionStatus.SETTLED,
                date=tx_date,
                settled_date=tx_date,
            )
            db.add(tx)
            tx_count += 1

    await db.flush()

    logger.info(
        "demo_seed.completed",
        org_id=org_id,
        accounts=len(accounts),
        transactions=tx_count,
    )

    return SeedResult(
        accounts_created=len(accounts),
        transactions_created=tx_count,
        categories_created=1,
    )
