"""Tests for forecast_plan_service._compute_actuals_batch — pins the rules
that align ForecastPlan actuals with budget_service spent computation:
settled_date for period assignment, transfer halves excluded, subcategories
rolled into master, settled-status only.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.account import Account, AccountType
from app.models.billing import BillingPeriod
from app.models.category import Category, CategoryType
from app.models.forecast_plan import (
    ForecastItemType,
    ForecastPlan,
    ForecastPlanItem,
    ItemSource,
    PlanStatus,
)
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization
from app.services import forecast_plan_service


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed(factory: async_sessionmaker[AsyncSession]) -> dict:
    """Org + account + Groceries master/sub + April + May periods + plan."""
    org_id = 1
    april_start = datetime.date(2026, 4, 1)
    april_end = datetime.date(2026, 4, 30)
    may_start = datetime.date(2026, 5, 1)

    async with factory() as db:
        db.add(Organization(id=org_id, name="org", billing_cycle_day=1))
        await db.commit()

        at = AccountType(org_id=org_id, name="Cash", slug="cash", is_system=True)
        db.add(at)
        await db.commit()

        acc = Account(org_id=org_id, account_type_id=at.id, name="Wallet", balance=Decimal("0"))
        db.add(acc)
        await db.commit()

        master = Category(org_id=org_id, name="Groceries", slug="groceries", type=CategoryType.EXPENSE)
        db.add(master)
        await db.commit()
        sub = Category(
            org_id=org_id, parent_id=master.id, name="Supermarket",
            slug="supermarket", type=CategoryType.EXPENSE,
        )
        db.add(sub)
        await db.commit()

        april = BillingPeriod(org_id=org_id, start_date=april_start, end_date=april_end)
        may = BillingPeriod(org_id=org_id, start_date=may_start, end_date=None)
        db.add_all([april, may])
        await db.commit()

        plan = ForecastPlan(org_id=org_id, billing_period_id=april.id, status=PlanStatus.DRAFT)
        db.add(plan)
        await db.commit()
        item = ForecastPlanItem(
            plan_id=plan.id, org_id=org_id, category_id=master.id,
            type=ForecastItemType.EXPENSE, planned_amount=Decimal("500"),
            source=ItemSource.MANUAL,
        )
        db.add(item)
        await db.commit()

        return {
            "org_id": org_id,
            "account_id": acc.id,
            "master_id": master.id,
            "sub_id": sub.id,
            "april_start": april_start,
            "april_end": april_end,
            "may_start": may_start,
            "item_id": item.id,
        }


def _tx(*, org_id, account_id, category_id, amount, date, settled_date, status, type_,
        linked_transaction_id=None) -> Transaction:
    return Transaction(
        org_id=org_id, account_id=account_id, category_id=category_id,
        description="t", amount=Decimal(str(amount)), type=type_, status=status,
        date=date, settled_date=settled_date, linked_transaction_id=linked_transaction_id,
    )


@pytest.mark.asyncio
async def test_actuals_apply_budget_rules(session_factory):
    """One scenario covering all four invariants the normalization pins.

    Master is "Groceries"; sub is "Supermarket". Plan is for April.
    Transactions seeded:
      T1  expense $100  date=Apr 28, settled_date=May 2  — CC swipe; belongs to MAY
      T2  expense  $50  date=Apr 5,  settled_date=Apr 5  — cash; belongs to APRIL
      T3  expense $200  date=Apr 10, settled_date=Apr 10, on SUB — rolls to MASTER in April
      T4a expense $300  settled in Apr, transfer source                        — excluded
      T4b income  $300  settled in Apr, paired transfer (linked_transaction_id) — excluded
      T5  expense  $40  date=Apr 8,  settled_date=None, status=PENDING         — excluded

    Expected April actual for (master, expense) = T2 + T3 = $250.
    Expected May  actual for (master, expense) = T1 = $100.
    """
    seed = await _seed(session_factory)

    async with session_factory() as db:
        # Settled cash purchase in April
        db.add(_tx(
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=seed["master_id"], amount=50,
            date=datetime.date(2026, 4, 5), settled_date=datetime.date(2026, 4, 5),
            status=TransactionStatus.SETTLED, type_=TransactionType.EXPENSE,
        ))
        # Settled subcategory purchase in April — must roll to master
        db.add(_tx(
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=seed["sub_id"], amount=200,
            date=datetime.date(2026, 4, 10), settled_date=datetime.date(2026, 4, 10),
            status=TransactionStatus.SETTLED, type_=TransactionType.EXPENSE,
        ))
        # CC swipe on Apr 28, settles May 2 — must NOT count for April
        db.add(_tx(
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=seed["master_id"], amount=100,
            date=datetime.date(2026, 4, 28), settled_date=datetime.date(2026, 5, 2),
            status=TransactionStatus.SETTLED, type_=TransactionType.EXPENSE,
        ))
        # Pending — must be excluded
        db.add(_tx(
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=seed["master_id"], amount=40,
            date=datetime.date(2026, 4, 8), settled_date=None,
            status=TransactionStatus.PENDING, type_=TransactionType.EXPENSE,
        ))
        await db.commit()

        # Transfer pair — both halves excluded by linked_transaction_id IS NULL
        a = _tx(
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=seed["master_id"], amount=300,
            date=datetime.date(2026, 4, 15), settled_date=datetime.date(2026, 4, 15),
            status=TransactionStatus.SETTLED, type_=TransactionType.EXPENSE,
        )
        db.add(a)
        await db.commit()
        b = _tx(
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=seed["master_id"], amount=300,
            date=datetime.date(2026, 4, 15), settled_date=datetime.date(2026, 4, 15),
            status=TransactionStatus.SETTLED, type_=TransactionType.INCOME,
            linked_transaction_id=a.id,
        )
        db.add(b)
        await db.commit()
        a.linked_transaction_id = b.id
        await db.commit()

    # April actuals
    async with session_factory() as db:
        item = ForecastPlanItem(
            id=seed["item_id"], plan_id=1, org_id=seed["org_id"],
            category_id=seed["master_id"], type=ForecastItemType.EXPENSE,
            planned_amount=Decimal("500"), source=ItemSource.MANUAL,
        )
        actuals = await forecast_plan_service._compute_actuals_batch(
            db, org_id=seed["org_id"], items=[item],
            period_start=seed["april_start"], period_end=seed["april_end"],
        )
    assert actuals.get((seed["master_id"], "expense")) == Decimal("250")
    # Transfer income half must not surface as an income actual either
    assert (seed["master_id"], "income") not in actuals

    # May actuals — only the CC swipe that settled May 2
    async with session_factory() as db:
        item = ForecastPlanItem(
            id=seed["item_id"], plan_id=1, org_id=seed["org_id"],
            category_id=seed["master_id"], type=ForecastItemType.EXPENSE,
            planned_amount=Decimal("500"), source=ItemSource.MANUAL,
        )
        actuals = await forecast_plan_service._compute_actuals_batch(
            db, org_id=seed["org_id"], items=[item],
            period_start=seed["may_start"], period_end=None,
        )
    assert actuals.get((seed["master_id"], "expense")) == Decimal("100")
