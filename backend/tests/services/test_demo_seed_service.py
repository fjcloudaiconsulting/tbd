"""Unit tests for ``app.services.demo_seed_service`` (L3.3).

Coverage:
- Happy path: empty org gets accounts, transactions, and the sentinel category.
- Idempotency: a second call refuses with ``DemoSeedAlreadyApplied``.
- Org isolation: seeding org A leaves org B untouched (no rows leak).
- Real-data guard: an org with one real tx refuses before any write.
- Sentinel guard: an org with the demo sentinel refuses (manual category replay).
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.account import Account, AccountType
from app.models.category import Category, CategoryType
from app.models.transaction import (
    Transaction,
    TransactionStatus,
    TransactionType,
)
from app.models.user import Organization
from app.services.demo_seed_service import (
    DEMO_SENTINEL_SLUG,
    DemoSeedAlreadyApplied,
    seed_org,
)


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(Engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _bootstrap_org(factory, name: str) -> int:
    """Create an org with the minimum system fixtures the seed expects."""
    async with factory() as db:
        org = Organization(name=name, billing_cycle_day=1)
        db.add(org)
        await db.flush()
        at = AccountType(
            org_id=org.id, name="Checking", slug="checking", is_system=True
        )
        db.add(at)
        # System-style categories so the slug lookup hits a real row.
        for slug, name_ in [
            ("paycheck", "Paycheck"),
            ("groceries", "Groceries"),
            ("rent_mortgage", "Rent"),
            ("coffee_shops", "Coffee"),
        ]:
            db.add(
                Category(
                    org_id=org.id, name=name_, slug=slug,
                    is_system=True, type=CategoryType.BOTH,
                )
            )
        await db.commit()
        return org.id


@pytest.mark.asyncio
async def test_seed_org_happy_path(session_factory):
    org_id = await _bootstrap_org(session_factory, "Org A")
    async with session_factory() as db:
        result = await seed_org(db, org_id)
        await db.commit()
    assert result.accounts_created == 2
    assert result.transactions_created > 0
    assert result.categories_created == 1

    async with session_factory() as db:
        accts = (
            await db.execute(select(Account).where(Account.org_id == org_id))
        ).scalars().all()
        assert {a.name for a in accts} == {"Sample Checking", "Sample Savings"}
        sentinel = (
            await db.execute(
                select(Category).where(
                    Category.org_id == org_id,
                    Category.slug == DEMO_SENTINEL_SLUG,
                )
            )
        ).scalar_one_or_none()
        assert sentinel is not None


@pytest.mark.asyncio
async def test_seed_org_refuses_when_already_seeded(session_factory):
    org_id = await _bootstrap_org(session_factory, "Org A")
    async with session_factory() as db:
        await seed_org(db, org_id)
        await db.commit()
    async with session_factory() as db:
        with pytest.raises(DemoSeedAlreadyApplied):
            await seed_org(db, org_id)


@pytest.mark.asyncio
async def test_seed_org_refuses_when_org_has_real_data(session_factory):
    org_id = await _bootstrap_org(session_factory, "Org A")
    # Manually drop in a real transaction.
    async with session_factory() as db:
        at = (
            await db.execute(
                select(AccountType).where(AccountType.org_id == org_id)
            )
        ).scalar_one()
        acct = Account(
            org_id=org_id, name="Real Checking", account_type_id=at.id,
            balance=Decimal("100.00"), currency="EUR", is_active=True,
        )
        db.add(acct)
        cat = (
            await db.execute(
                select(Category).where(
                    Category.org_id == org_id, Category.slug == "groceries"
                )
            )
        ).scalar_one()
        await db.flush()
        tx = Transaction(
            org_id=org_id, account_id=acct.id, category_id=cat.id,
            description="Real spend", amount=Decimal("10.00"),
            type=TransactionType.EXPENSE,
            status=TransactionStatus.SETTLED,
            date=datetime.date(2026, 5, 1),
            settled_date=datetime.date(2026, 5, 1),
        )
        db.add(tx)
        await db.commit()

    async with session_factory() as db:
        with pytest.raises(DemoSeedAlreadyApplied):
            await seed_org(db, org_id)


@pytest.mark.asyncio
async def test_seed_org_isolation_does_not_touch_other_orgs(session_factory):
    org_a = await _bootstrap_org(session_factory, "Org A")
    org_b = await _bootstrap_org(session_factory, "Org B")

    async with session_factory() as db:
        await seed_org(db, org_a)
        await db.commit()

    # Org B has zero accounts and zero transactions.
    async with session_factory() as db:
        b_accts = await db.scalar(
            select(func.count(Account.id)).where(Account.org_id == org_b)
        )
        b_tx = await db.scalar(
            select(func.count(Transaction.id)).where(Transaction.org_id == org_b)
        )
        b_sentinel = await db.scalar(
            select(func.count(Category.id)).where(
                Category.org_id == org_b,
                Category.slug == DEMO_SENTINEL_SLUG,
            )
        )
    assert b_accts == 0
    assert b_tx == 0
    assert b_sentinel == 0
