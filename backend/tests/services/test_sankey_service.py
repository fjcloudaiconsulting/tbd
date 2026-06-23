"""Builder tests for the cash-flow Sankey service.

Pinned invariants:

- Income categories produce source → "Income" links.
- Spending categories produce "Income" → target links.
- ``Income → Savings`` appears when income > expense.
- Transfer pairs (``linked_transaction_id`` IS NOT NULL) contribute
  NOTHING to any link — the builder applies
  ``reportable_transaction_filter()`` which excludes transfer legs.
- Manual-adjustment rows are also excluded.
- ``income_total == 0`` → empty ``links`` list.
- ``top_n`` folds the smallest spending categories into "Income → Other".
- ``spending_granularity="category_master"`` groups by parent category.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.account import Account, AccountType
from app.models.category import Category
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization, Role, User
from app.schemas.reports_query import SankeyQuery
from app.security import hash_password
from app.services.sankey_service import build_sankey


# ── fixtures ─────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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


async def _seed_world(factory) -> dict:
    """Seed org with:
    - 2 income txns: Salary 5000, Freelance 1000
    - 3 expense txns: Housing 2000, Food 800, Transport 400
    - 1 transfer pair (two linked rows, 500 each — income + expense side)
    - 1 manual-adjustment row (should be excluded)

    Returns a dict of IDs for assertions.
    """
    async with factory() as db:
        org = Organization(name="Test Org", billing_cycle_day=1)
        db.add(org)
        await db.commit()

        user = User(
            org_id=org.id,
            username="test_user",
            email="test@example.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            email_verified=True,
        )
        db.add(user)
        await db.commit()

        at = AccountType(org_id=org.id, name="Checking")
        db.add(at)
        await db.commit()

        acct = Account(
            org_id=org.id,
            account_type_id=at.id,
            name="Main Bank",
            currency="EUR",
            balance=Decimal("0"),
        )
        db.add(acct)
        await db.commit()

        # Income categories
        cat_salary = Category(org_id=org.id, name="Salary")
        cat_freelance = Category(org_id=org.id, name="Freelance")
        # Expense categories
        cat_housing = Category(org_id=org.id, name="Housing")
        cat_food = Category(org_id=org.id, name="Food")
        cat_transport = Category(org_id=org.id, name="Transport")
        db.add_all([cat_salary, cat_freelance, cat_housing, cat_food, cat_transport])
        await db.commit()

        today = date(2026, 6, 1)

        # Income transactions
        tx_salary = Transaction(
            org_id=org.id,
            account_id=acct.id,
            category_id=cat_salary.id,
            description="Salary",
            amount=Decimal("5000"),
            type=TransactionType.INCOME,
            status=TransactionStatus.SETTLED,
            date=today,
            settled_date=today,
        )
        tx_freelance = Transaction(
            org_id=org.id,
            account_id=acct.id,
            category_id=cat_freelance.id,
            description="Freelance",
            amount=Decimal("1000"),
            type=TransactionType.INCOME,
            status=TransactionStatus.SETTLED,
            date=today,
            settled_date=today,
        )

        # Expense transactions
        tx_housing = Transaction(
            org_id=org.id,
            account_id=acct.id,
            category_id=cat_housing.id,
            description="Housing",
            amount=Decimal("2000"),
            type=TransactionType.EXPENSE,
            status=TransactionStatus.SETTLED,
            date=today,
            settled_date=today,
        )
        tx_food = Transaction(
            org_id=org.id,
            account_id=acct.id,
            category_id=cat_food.id,
            description="Food",
            amount=Decimal("800"),
            type=TransactionType.EXPENSE,
            status=TransactionStatus.SETTLED,
            date=today,
            settled_date=today,
        )
        tx_transport = Transaction(
            org_id=org.id,
            account_id=acct.id,
            category_id=cat_transport.id,
            description="Transport",
            amount=Decimal("400"),
            type=TransactionType.EXPENSE,
            status=TransactionStatus.SETTLED,
            date=today,
            settled_date=today,
        )
        db.add_all([tx_salary, tx_freelance, tx_housing, tx_food, tx_transport])
        await db.flush()

        # Transfer pair: two linked rows. The income leg has a
        # linked_transaction_id pointing to the expense leg and vice-versa.
        # reportable_transaction_filter() excludes both because
        # linked_transaction_id IS NOT NULL on each.
        tx_transfer_income = Transaction(
            org_id=org.id,
            account_id=acct.id,
            category_id=cat_salary.id,  # would inflate Salary if not excluded
            description="Transfer in",
            amount=Decimal("500"),
            type=TransactionType.INCOME,
            status=TransactionStatus.SETTLED,
            date=today,
            settled_date=today,
        )
        tx_transfer_expense = Transaction(
            org_id=org.id,
            account_id=acct.id,
            category_id=cat_housing.id,  # would inflate Housing if not excluded
            description="Transfer out",
            amount=Decimal("500"),
            type=TransactionType.EXPENSE,
            status=TransactionStatus.SETTLED,
            date=today,
            settled_date=today,
        )
        db.add_all([tx_transfer_income, tx_transfer_expense])
        await db.flush()

        # Wire the pair bidirectionally.
        tx_transfer_income.linked_transaction_id = tx_transfer_expense.id
        tx_transfer_expense.linked_transaction_id = tx_transfer_income.id
        await db.flush()

        # Manual adjustment — also excluded by reportable_transaction_filter.
        tx_manual = Transaction(
            org_id=org.id,
            account_id=acct.id,
            category_id=cat_salary.id,
            description="Manual adj",
            amount=Decimal("9999"),
            type=TransactionType.INCOME,
            status=TransactionStatus.SETTLED,
            date=today,
            settled_date=today,
            is_manual_adjustment=True,
        )
        db.add(tx_manual)
        await db.commit()

        return {
            "org_id": org.id,
            "user_id": user.id,
        }


# ── tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_basic_links_and_savings(session_factory):
    """Core invariants: income links, expense links, Savings link.

    Seeded totals:
        income_total  = 5000 + 1000 = 6000
        expense_total = 2000 + 800 + 400 = 3200
        Savings       = 6000 - 3200 = 2800
    """
    world = await _seed_world(session_factory)
    async with session_factory() as db:
        response = await build_sankey(db, org_id=world["org_id"], query=SankeyQuery(filters=[]))

    links = response.links
    assert links, "Expected non-empty links"

    by = {(lk.source, lk.target): lk.value for lk in links}

    # Income → hub
    assert by[("Salary", "Income")] == pytest.approx(5000.0)
    assert by[("Freelance", "Income")] == pytest.approx(1000.0)

    # Hub → spending
    assert by[("Income", "Housing")] == pytest.approx(2000.0)
    assert by[("Income", "Food")] == pytest.approx(800.0)
    assert by[("Income", "Transport")] == pytest.approx(400.0)

    # Savings
    assert by[("Income", "Savings")] == pytest.approx(2800.0)


@pytest.mark.asyncio
async def test_transfer_pair_excluded(session_factory):
    """Transfer pair must contribute NOTHING to any link totals.

    The transfer income leg is in "Salary" category (amount 500).
    The transfer expense leg is in "Housing" category (amount 500).
    If not excluded, Salary would show 5500 and Housing 2500.
    The totals must remain 5000 and 2000 respectively.
    """
    world = await _seed_world(session_factory)
    async with session_factory() as db:
        response = await build_sankey(db, org_id=world["org_id"], query=SankeyQuery(filters=[]))

    by = {(lk.source, lk.target): lk.value for lk in response.links}

    # Transfer legs must NOT inflate their categories.
    assert by[("Salary", "Income")] == pytest.approx(5000.0), (
        "Transfer income leg (500) must not be counted in Salary"
    )
    assert by[("Income", "Housing")] == pytest.approx(2000.0), (
        "Transfer expense leg (500) must not be counted in Housing"
    )

    # Income total must equal 6000 (not 6500 with the transfer leg).
    income_total = sum(lk.value for lk in response.links if lk.target == "Income")
    assert income_total == pytest.approx(6000.0), (
        "Transfer legs must not inflate income total"
    )

    # Expense total out of hub must equal 3200 + 2800 savings = 6000.
    outflows = sum(lk.value for lk in response.links if lk.source == "Income")
    assert outflows == pytest.approx(6000.0)


@pytest.mark.asyncio
async def test_empty_when_no_income(session_factory):
    """When income_total == 0, return empty links (frontend empty-state)."""
    async with session_factory() as db:
        org = Organization(name="Empty Org", billing_cycle_day=1)
        db.add(org)
        await db.commit()

        response = await build_sankey(db, org_id=org.id, query=SankeyQuery(filters=[]))

    assert response.links == []
    assert response.meta.row_count == 0


@pytest.mark.asyncio
async def test_no_savings_link_when_expense_exceeds_income(session_factory):
    """When expense >= income, no Savings link is produced."""
    async with session_factory() as db:
        org = Organization(name="Overspend Org", billing_cycle_day=1)
        db.add(org)
        await db.commit()

        at = AccountType(org_id=org.id, name="Checking")
        db.add(at)
        await db.commit()

        acct = Account(
            org_id=org.id,
            account_type_id=at.id,
            name="Bank",
            currency="EUR",
            balance=Decimal("0"),
        )
        db.add(acct)
        await db.commit()

        cat_income = Category(org_id=org.id, name="Salary")
        cat_expense = Category(org_id=org.id, name="Rent")
        db.add_all([cat_income, cat_expense])
        await db.commit()

        today = date(2026, 6, 1)
        db.add(
            Transaction(
                org_id=org.id,
                account_id=acct.id,
                category_id=cat_income.id,
                description="Salary",
                amount=Decimal("1000"),
                type=TransactionType.INCOME,
                status=TransactionStatus.SETTLED,
                date=today,
                settled_date=today,
            )
        )
        db.add(
            Transaction(
                org_id=org.id,
                account_id=acct.id,
                category_id=cat_expense.id,
                description="Rent",
                amount=Decimal("1500"),
                type=TransactionType.EXPENSE,
                status=TransactionStatus.SETTLED,
                date=today,
                settled_date=today,
            )
        )
        await db.commit()

        response = await build_sankey(db, org_id=org.id, query=SankeyQuery(filters=[]))

    by_key = {(lk.source, lk.target) for lk in response.links}
    assert ("Income", "Savings") not in by_key


@pytest.mark.asyncio
async def test_top_n_folds_tail_into_other(session_factory):
    """top_n=2 keeps the two largest spending categories, folds rest → Other."""
    world = await _seed_world(session_factory)
    async with session_factory() as db:
        response = await build_sankey(
            db, org_id=world["org_id"], query=SankeyQuery(filters=[], top_n=2)
        )

    # Spending side: Housing 2000, Food 800, Transport 400.
    # Top 2 = Housing + Food. Other = 400.
    by = {(lk.source, lk.target): lk.value for lk in response.links}

    assert ("Income", "Housing") in by
    assert ("Income", "Food") in by
    assert ("Income", "Transport") not in by
    assert by[("Income", "Other")] == pytest.approx(400.0)


@pytest.mark.asyncio
async def test_spending_granularity_category_master(session_factory):
    """spending_granularity='category_master' groups by parent category.

    Seed: parent "Bills" with children "Housing" (2000) and "Transport" (400).
    Food (800) has no parent → should group under its own name.
    """
    async with session_factory() as db:
        org = Organization(name="Master Org", billing_cycle_day=1)
        db.add(org)
        await db.commit()

        at = AccountType(org_id=org.id, name="Checking")
        db.add(at)
        await db.commit()

        acct = Account(
            org_id=org.id,
            account_type_id=at.id,
            name="Bank",
            currency="EUR",
            balance=Decimal("0"),
        )
        db.add(acct)
        await db.commit()

        # Category hierarchy
        cat_salary = Category(org_id=org.id, name="Salary")
        cat_bills = Category(org_id=org.id, name="Bills")  # parent
        db.add_all([cat_salary, cat_bills])
        await db.flush()

        cat_housing = Category(org_id=org.id, name="Housing", parent_id=cat_bills.id)
        cat_transport = Category(org_id=org.id, name="Transport", parent_id=cat_bills.id)
        cat_food = Category(org_id=org.id, name="Food")  # no parent
        db.add_all([cat_housing, cat_transport, cat_food])
        await db.commit()

        today = date(2026, 6, 1)
        db.add(
            Transaction(
                org_id=org.id, account_id=acct.id, category_id=cat_salary.id,
                description="Salary", amount=Decimal("5000"),
                type=TransactionType.INCOME, status=TransactionStatus.SETTLED,
                date=today, settled_date=today,
            )
        )
        db.add(
            Transaction(
                org_id=org.id, account_id=acct.id, category_id=cat_housing.id,
                description="Housing", amount=Decimal("2000"),
                type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
                date=today, settled_date=today,
            )
        )
        db.add(
            Transaction(
                org_id=org.id, account_id=acct.id, category_id=cat_transport.id,
                description="Transport", amount=Decimal("400"),
                type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
                date=today, settled_date=today,
            )
        )
        db.add(
            Transaction(
                org_id=org.id, account_id=acct.id, category_id=cat_food.id,
                description="Food", amount=Decimal("800"),
                type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
                date=today, settled_date=today,
            )
        )
        await db.commit()

        response = await build_sankey(
            db,
            org_id=org.id,
            query=SankeyQuery(filters=[], spending_granularity="category_master"),
        )

    by = {(lk.source, lk.target): lk.value for lk in response.links}

    # Housing + Transport should roll up to "Bills" (their parent)
    assert by[("Income", "Bills")] == pytest.approx(2400.0)
    # Food has no parent → stays as "Food"
    assert by[("Income", "Food")] == pytest.approx(800.0)
    # No "Housing" or "Transport" as direct keys
    assert ("Income", "Housing") not in by
    assert ("Income", "Transport") not in by
