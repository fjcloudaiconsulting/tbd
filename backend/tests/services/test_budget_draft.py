"""AI next-period budget draft (deterministic projection).

``suggest_next_period_budget`` proposes a next-period budget per expense
category from its trailing 3-complete-month spend average (next period
has no actuals, so the projection is the average). No LLM, no entitlement
gate — pure arithmetic. Offline: in-memory sqlite.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.account import Account, AccountType
from app.models.billing import BillingPeriod
from app.models.budget import Budget
from app.models.category import Category, CategoryType
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization
from app.services import budget_draft_service as svc
from app.services.billing_service import ensure_future_periods

# Anchor to the container's current month so the trailing-3-month window
# ([current_month_start - 3mo, current_month_start)) captures the seeded
# history deterministically.
TODAY = datetime.date.today()
CURRENT_MONTH_START = TODAY.replace(day=1)
NEXT_PERIOD_START = (CURRENT_MONTH_START + datetime.timedelta(days=32)).replace(day=1)


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


async def _seed(factory, *, with_history: bool) -> dict:
    org_id = 1
    async with factory() as db:
        db.add(Organization(id=org_id, name="org", billing_cycle_day=1))
        await db.commit()
        db.add(BillingPeriod(org_id=org_id, start_date=CURRENT_MONTH_START, end_date=None))
        await db.commit()
        groceries = Category(org_id=org_id, name="Groceries", slug="g", type=CategoryType.EXPENSE)
        db.add(groceries)
        await db.commit()
        at = AccountType(org_id=org_id, name="Checking", slug="checking")
        db.add(at)
        await db.flush()
        acct = Account(
            org_id=org_id, account_type_id=at.id, name="checking",
            balance=Decimal("1000.00"), currency="USD",
        )
        db.add(acct)
        await db.commit()
        cats = {"groceries": groceries.id}
        if with_history:
            # 3 settled expenses of $300 in each of the 3 trailing months.
            for months_back in (1, 2, 3):
                d = (CURRENT_MONTH_START - datetime.timedelta(days=1))
                for _ in range(months_back - 1):
                    d = (d.replace(day=1) - datetime.timedelta(days=1))
                settled = d.replace(day=15)
                db.add(Transaction(
                    org_id=org_id, account_id=acct.id, category_id=groceries.id,
                    description="x", amount=Decimal("300.00"),
                    type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
                    settled_date=settled, date=settled,
                ))
            await db.commit()
        return {"org_id": org_id, "cats": cats}


@pytest.mark.asyncio
async def test_draft_projects_next_period_from_trailing_average(session_factory):
    seed = await _seed(session_factory, with_history=True)
    org_id = seed["org_id"]
    async with session_factory() as db:
        await ensure_future_periods(db, org_id)
    async with session_factory() as db:
        out = await svc.suggest_next_period_budget(
            db, org_id, period_start=NEXT_PERIOD_START
        )
    assert out.status == "ok"
    assert out.period_start == NEXT_PERIOD_START
    by_cat = {s.category_id: s for s in out.suggestions}
    g = by_cat[seed["cats"]["groceries"]]
    # $900 over 3 months → $300 average → projected next-period budget.
    assert g.suggested_amount == Decimal("300.00")
    assert g.current_amount == Decimal("0.00")
    assert g.delta_amount == Decimal("300.00")


@pytest.mark.asyncio
async def test_draft_skips_categories_already_budgeted_in_target(session_factory):
    seed = await _seed(session_factory, with_history=True)
    org_id = seed["org_id"]
    async with session_factory() as db:
        await ensure_future_periods(db, org_id)
        # A budget already exists for groceries in the target period.
        db.add(Budget(
            org_id=org_id, category_id=seed["cats"]["groceries"],
            amount=Decimal("123.00"), period_start=NEXT_PERIOD_START, period_end=None,
        ))
        await db.commit()
    async with session_factory() as db:
        out = await svc.suggest_next_period_budget(
            db, org_id, period_start=NEXT_PERIOD_START
        )
    assert all(s.category_id != seed["cats"]["groceries"] for s in out.suggestions)


@pytest.mark.asyncio
async def test_draft_with_no_history_is_empty(session_factory):
    seed = await _seed(session_factory, with_history=False)
    org_id = seed["org_id"]
    async with session_factory() as db:
        await ensure_future_periods(db, org_id)
    async with session_factory() as db:
        out = await svc.suggest_next_period_budget(
            db, org_id, period_start=NEXT_PERIOD_START
        )
    assert out.status == "empty_no_history"
    assert out.suggestions == []
