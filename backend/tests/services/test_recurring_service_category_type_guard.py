"""Server-side guard: recurring template writes must enforce the same
(type, category) compatibility rule that single-transaction writes enforce.

Closes the HIGH finding from PR #150 round 2: recurring_service.create_recurring
called validate_category (existence only), and update_recurring let type and
category_id change independently with no compatibility check. Because
generate_due_transactions writes Transaction rows directly from the template
(not via _create_transaction_no_commit), a mismatched template would emit
mismatched rows that bypass the new guard at every cycle.

Rule: validate the resolved (type, category) pair on create AND on the
post-update state for update.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.category import CategoryType
from app.schemas.recurring import RecurringCreate, RecurringUpdate
from app.services import recurring_service
from app.services.exceptions import ValidationError


@pytest_asyncio.fixture
async def db_session():
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
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed(db: AsyncSession) -> dict:
    org = Organization(name="T", billing_cycle_day=1)
    db.add(org)
    await db.flush()
    at = AccountType(
        org_id=org.id, name="Checking", slug="checking", is_system=True
    )
    db.add(at)
    await db.flush()
    acct = Account(
        org_id=org.id, name="Main", account_type_id=at.id,
        balance=Decimal("0"), currency="EUR",
    )
    db.add(acct)
    await db.flush()

    expense_master = Category(
        org_id=org.id, name="Groceries", slug="groceries",
        type=CategoryType.EXPENSE,
    )
    income_master = Category(
        org_id=org.id, name="Salary", slug="salary",
        type=CategoryType.INCOME,
    )
    both_master = Category(
        org_id=org.id, name="Misc", slug="misc",
        type=CategoryType.BOTH,
    )
    db.add_all([expense_master, income_master, both_master])
    await db.commit()

    return {
        "org_id": org.id,
        "account_id": acct.id,
        "expense_cat_id": expense_master.id,
        "income_cat_id": income_master.id,
        "both_cat_id": both_master.id,
    }


# ── create_recurring ───────────────────────────────────────────────────────


async def test_create_recurring_rejects_income_with_expense_category(db_session):
    seed = await _seed(db_session)
    body = RecurringCreate(
        account_id=seed["account_id"],
        category_id=seed["expense_cat_id"],
        description="paycheck",
        amount=Decimal("100"),
        type="income",
        frequency="monthly",
        next_due_date=date(2026, 6, 1),
    )
    with pytest.raises(ValidationError):
        await recurring_service.create_recurring(
            db_session, seed["org_id"], body
        )


async def test_create_recurring_rejects_expense_with_income_category(db_session):
    seed = await _seed(db_session)
    body = RecurringCreate(
        account_id=seed["account_id"],
        category_id=seed["income_cat_id"],
        description="rent",
        amount=Decimal("500"),
        type="expense",
        frequency="monthly",
        next_due_date=date(2026, 6, 1),
    )
    with pytest.raises(ValidationError):
        await recurring_service.create_recurring(
            db_session, seed["org_id"], body
        )


async def test_create_recurring_accepts_matching_pair(db_session):
    seed = await _seed(db_session)
    body = RecurringCreate(
        account_id=seed["account_id"],
        category_id=seed["expense_cat_id"],
        description="rent",
        amount=Decimal("500"),
        type="expense",
        frequency="monthly",
        next_due_date=date(2026, 6, 1),
    )
    r = await recurring_service.create_recurring(
        db_session, seed["org_id"], body
    )
    assert r.id is not None


async def test_create_recurring_accepts_both_category(db_session):
    """CategoryType.BOTH is compatible with either type."""
    seed = await _seed(db_session)
    body = RecurringCreate(
        account_id=seed["account_id"],
        category_id=seed["both_cat_id"],
        description="x",
        amount=Decimal("1"),
        type="expense",
        frequency="monthly",
        next_due_date=date(2026, 6, 1),
    )
    r = await recurring_service.create_recurring(
        db_session, seed["org_id"], body
    )
    assert r.id is not None


# ── update_recurring ───────────────────────────────────────────────────────


async def _create_compatible(db: AsyncSession, seed: dict, *, type_: str = "expense"):
    cat_id = seed["expense_cat_id"] if type_ == "expense" else seed["income_cat_id"]
    body = RecurringCreate(
        account_id=seed["account_id"],
        category_id=cat_id,
        description="initial",
        amount=Decimal("10"),
        type=type_,
        frequency="monthly",
        next_due_date=date(2026, 6, 1),
    )
    return await recurring_service.create_recurring(db, seed["org_id"], body)


async def test_update_recurring_rejects_category_only_swap_to_incompatible(db_session):
    """Existing expense template, swap category to income-only → reject."""
    seed = await _seed(db_session)
    r = await _create_compatible(db_session, seed, type_="expense")
    upd = RecurringUpdate(category_id=seed["income_cat_id"])
    with pytest.raises(ValidationError):
        await recurring_service.update_recurring(
            db_session, seed["org_id"], r.id, upd
        )


async def test_update_recurring_rejects_type_only_swap_to_incompatible(db_session):
    """Existing expense template (expense category), flip type to income → reject."""
    seed = await _seed(db_session)
    r = await _create_compatible(db_session, seed, type_="expense")
    upd = RecurringUpdate(type="income")
    with pytest.raises(ValidationError):
        await recurring_service.update_recurring(
            db_session, seed["org_id"], r.id, upd
        )


async def test_update_recurring_accepts_compatible_simultaneous_swap(db_session):
    """type AND category change together to a compatible pair → accept."""
    seed = await _seed(db_session)
    r = await _create_compatible(db_session, seed, type_="expense")
    upd = RecurringUpdate(type="income", category_id=seed["income_cat_id"])
    updated = await recurring_service.update_recurring(
        db_session, seed["org_id"], r.id, upd
    )
    assert updated.type == "income"
    assert updated.category_id == seed["income_cat_id"]


async def test_update_recurring_rejects_incompatible_simultaneous_swap(db_session):
    """type and category both change but resulting pair is incompatible."""
    seed = await _seed(db_session)
    r = await _create_compatible(db_session, seed, type_="expense")
    upd = RecurringUpdate(type="income", category_id=seed["expense_cat_id"])
    with pytest.raises(ValidationError):
        await recurring_service.update_recurring(
            db_session, seed["org_id"], r.id, upd
        )


async def test_update_recurring_accepts_swap_to_both_category(db_session):
    """Swap to a CategoryType.BOTH category is always compatible."""
    seed = await _seed(db_session)
    r = await _create_compatible(db_session, seed, type_="expense")
    upd = RecurringUpdate(category_id=seed["both_cat_id"])
    updated = await recurring_service.update_recurring(
        db_session, seed["org_id"], r.id, upd
    )
    assert updated.category_id == seed["both_cat_id"]
