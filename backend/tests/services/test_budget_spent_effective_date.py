"""Budget spend bucketing aligns to effective_period_date_expr().

For SETTLED rows (the only ones budget spend counts) settled_date is always
populated, so switching the period predicate from a raw ``settled_date``
comparison to ``effective_period_date_expr()`` (= coalesce(settled_date,
date)) is behavior-identical. These tests are the regression guard for that
alignment: a GBLT (dated in May, settled in June) must count in its SETTLED
month, both before and after the swap.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.account import Account, AccountType
from app.models.category import Category, CategoryType
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization
from app.services import budget_service


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


async def _seed(factory) -> dict:
    async with factory() as db:
        org = Organization(name="org", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        at = AccountType(org_id=org.id, name="Cash", slug="cash", is_system=True)
        db.add(at)
        await db.commit()
        acc = Account(org_id=org.id, account_type_id=at.id, name="Wallet",
                      balance=Decimal("0"), currency="EUR")
        db.add(acc)
        await db.commit()
        cat = Category(org_id=org.id, name="Groceries", slug="groceries",
                       type=CategoryType.EXPENSE)
        db.add(cat)
        await db.commit()
        return {"org_id": org.id, "account_id": acc.id, "cat_id": cat.id}


@pytest.mark.asyncio
async def test_compute_spent_buckets_gblt_by_settled_month(session_factory):
    """A SETTLED expense dated 2026-05-31 but settled 2026-06-15 counts in
    the June budget period, not the May one.
    """
    seed = await _seed(session_factory)
    async with session_factory() as db:
        db.add(Transaction(
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=seed["cat_id"], description="GBLT",
            amount=Decimal("459.68"), type=TransactionType.EXPENSE,
            status=TransactionStatus.SETTLED,
            date=datetime.date(2026, 5, 31),
            settled_date=datetime.date(2026, 6, 15),
        ))
        await db.commit()

    async with session_factory() as db:
        june = await budget_service._compute_spent(
            db, seed["org_id"], seed["cat_id"],
            datetime.date(2026, 6, 1), datetime.date(2026, 6, 30),
        )
        may = await budget_service._compute_spent(
            db, seed["org_id"], seed["cat_id"],
            datetime.date(2026, 5, 1), datetime.date(2026, 5, 31),
        )
    assert june == Decimal("459.68")  # counted in its settled month
    assert may == Decimal("0")        # not in the purchase month
