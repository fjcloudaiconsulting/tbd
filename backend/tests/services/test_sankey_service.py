"""Builder tests for the cash-flow Sankey service.

Pinned invariants:

- Income categories produce source → HUB_INCOME links.
- Spending categories produce HUB_INCOME → target links.
- ``HUB_INCOME → HUB_SAVINGS`` appears when income > expense.
- Transfer pairs (``linked_transaction_id`` IS NOT NULL) contribute
  NOTHING to any link — the builder applies
  ``reportable_transaction_filter()`` which excludes transfer legs.
- Manual-adjustment rows are also excluded.
- ``income_total == 0`` → empty ``links`` list.
- ``top_n`` folds the smallest spending categories into
  ``HUB_INCOME → HUB_OTHER``.
- ``spending_granularity="category_master"`` groups by parent category.
- Real categories named "Income", "Savings", "Other" are NOT silenced
  by the sentinel collision guard — their txns flow correctly.
- A transfer-leg income + manual-adjustment income with a real expense
  → empty links (filter-driven empty, not row-absence).
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
from app.services.sankey_service import HUB_INCOME, HUB_OTHER, HUB_SAVINGS, build_sankey


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
    """Core invariants: income links, expense links, HUB_SAVINGS link.

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
    assert by[("Salary", HUB_INCOME)] == pytest.approx(5000.0)
    assert by[("Freelance", HUB_INCOME)] == pytest.approx(1000.0)

    # Hub → spending
    assert by[(HUB_INCOME, "Housing")] == pytest.approx(2000.0)
    assert by[(HUB_INCOME, "Food")] == pytest.approx(800.0)
    assert by[(HUB_INCOME, "Transport")] == pytest.approx(400.0)

    # Savings
    assert by[(HUB_INCOME, HUB_SAVINGS)] == pytest.approx(2800.0)


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
    assert by[("Salary", HUB_INCOME)] == pytest.approx(5000.0), (
        "Transfer income leg (500) must not be counted in Salary"
    )
    assert by[(HUB_INCOME, "Housing")] == pytest.approx(2000.0), (
        "Transfer expense leg (500) must not be counted in Housing"
    )

    # Income total must equal 6000 (not 6500 with the transfer leg).
    income_total = sum(lk.value for lk in response.links if lk.target == HUB_INCOME)
    assert income_total == pytest.approx(6000.0), (
        "Transfer legs must not inflate income total"
    )

    # Expense total out of hub must equal 3200 + 2800 savings = 6000.
    outflows = sum(lk.value for lk in response.links if lk.source == HUB_INCOME)
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
    """When expense >= income, no HUB_SAVINGS link is produced."""
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
    assert (HUB_INCOME, HUB_SAVINGS) not in by_key


@pytest.mark.asyncio
async def test_top_n_folds_tail_into_other(session_factory):
    """top_n=2 keeps the two largest spending categories, folds rest → HUB_OTHER."""
    world = await _seed_world(session_factory)
    async with session_factory() as db:
        response = await build_sankey(
            db, org_id=world["org_id"], query=SankeyQuery(filters=[], top_n=2)
        )

    # Spending side: Housing 2000, Food 800, Transport 400.
    # Top 2 = Housing + Food. Other = 400.
    by = {(lk.source, lk.target): lk.value for lk in response.links}

    assert (HUB_INCOME, "Housing") in by
    assert (HUB_INCOME, "Food") in by
    assert (HUB_INCOME, "Transport") not in by
    assert by[(HUB_INCOME, HUB_OTHER)] == pytest.approx(400.0)


@pytest.mark.asyncio
async def test_unsupported_filter_field_raises_value_error(session_factory):
    """_apply_user_filters raises ValueError for non-transaction filter fields.

    ``account_type`` is a valid FilterField enum value but is not in the
    Sankey-supported whitelist.  Passing it must raise ValueError (which the
    router maps to 422) rather than a KeyError (which would produce a 500).
    """
    world = await _seed_world(session_factory)
    from app.schemas.reports_query import Filter, FilterField, FilterOp

    bad_filter = Filter(field=FilterField.ACCOUNT_TYPE, op=FilterOp.EQ, value=1)
    async with session_factory() as db:
        with pytest.raises(ValueError, match="account_type"):
            await build_sankey(
                db,
                org_id=world["org_id"],
                query=SankeyQuery(filters=[bad_filter]),
            )


@pytest.mark.asyncio
async def test_row_count_reflects_pre_fold_counts(session_factory):
    """meta.row_count reports aggregated category counts BEFORE top_n folding.

    Seeded world: 2 income categories + 3 expense categories.
    With top_n=2, the expense side is folded to 2+Other, but row_count must
    still reflect 2 income + 3 expense = 5, not 2 + 2 = 4 (post-fold).
    """
    world = await _seed_world(session_factory)
    async with session_factory() as db:
        # Without folding: row_count should be 2 income + 3 expense = 5.
        response_no_fold = await build_sankey(
            db, org_id=world["org_id"], query=SankeyQuery(filters=[])
        )
    assert response_no_fold.meta.row_count == 5

    async with session_factory() as db:
        # With top_n=2: expense side folds to 2 visible + Other, but
        # row_count must still be 2 + 3 = 5 (pre-fold semantics).
        response_folded = await build_sankey(
            db, org_id=world["org_id"], query=SankeyQuery(filters=[], top_n=2)
        )
    assert response_folded.meta.row_count == 5

    # Structural: the fold bucket (HUB_INCOME → HUB_OTHER) and savings link
    # (HUB_INCOME → HUB_SAVINGS) must be present, pinning the tail>0 branch.
    folded_keys = {(lk.source, lk.target) for lk in response_folded.links}
    assert (HUB_INCOME, HUB_OTHER) in folded_keys, (
        "Folded tail bucket link (HUB_INCOME → HUB_OTHER) must be present"
    )
    assert (HUB_INCOME, HUB_SAVINGS) in folded_keys, (
        "Savings link (HUB_INCOME → HUB_SAVINGS) must be present"
    )


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
    assert by[(HUB_INCOME, "Bills")] == pytest.approx(2400.0)
    # Food has no parent → stays as "Food"
    assert by[(HUB_INCOME, "Food")] == pytest.approx(800.0)
    # No "Housing" or "Transport" as direct keys
    assert (HUB_INCOME, "Housing") not in by
    assert (HUB_INCOME, "Transport") not in by


# ── Fix 1: Hub/sentinel collision tests ──────────────────────────────


@pytest.mark.asyncio
async def test_category_named_income_flows_correctly(session_factory):
    """A real category named 'Income' must NOT be silenced by the self-loop guard.

    Before the sentinel fix, source == 'Income' was dropped with a guard
    that compared against the literal string 'Income'.  Now the guard
    compares against HUB_INCOME (the sentinel), so the real category flows.
    Similarly 'Savings' and 'Other' expense categories must not merge with
    the synthetic HUB_SAVINGS / HUB_OTHER sentinel buckets.
    """
    async with session_factory() as db:
        org = Organization(name="Collision Org", billing_cycle_day=1)
        db.add(org)
        await db.commit()

        at = AccountType(org_id=org.id, name="Checking")
        db.add(at)
        await db.commit()

        acct = Account(
            org_id=org.id, account_type_id=at.id,
            name="Bank", currency="EUR", balance=Decimal("0"),
        )
        db.add(acct)
        await db.commit()

        # Categories literally named after the old hub strings.
        cat_income_real = Category(org_id=org.id, name="Income")
        cat_savings_real = Category(org_id=org.id, name="Savings")
        cat_other_real = Category(org_id=org.id, name="Other")
        db.add_all([cat_income_real, cat_savings_real, cat_other_real])
        await db.commit()

        today = date(2026, 6, 1)
        # Income txn under the category literally named "Income"
        db.add(
            Transaction(
                org_id=org.id, account_id=acct.id,
                category_id=cat_income_real.id,
                description="Income cat txn", amount=Decimal("3000"),
                type=TransactionType.INCOME, status=TransactionStatus.SETTLED,
                date=today, settled_date=today,
            )
        )
        # Expense txns under categories literally named "Savings" and "Other"
        db.add(
            Transaction(
                org_id=org.id, account_id=acct.id,
                category_id=cat_savings_real.id,
                description="Savings expense", amount=Decimal("500"),
                type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
                date=today, settled_date=today,
            )
        )
        db.add(
            Transaction(
                org_id=org.id, account_id=acct.id,
                category_id=cat_other_real.id,
                description="Other expense", amount=Decimal("200"),
                type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
                date=today, settled_date=today,
            )
        )
        await db.commit()

        response = await build_sankey(db, org_id=org.id, query=SankeyQuery(filters=[]))

    by = {(lk.source, lk.target): lk.value for lk in response.links}

    # "Income" real category → hub: must NOT be dropped.
    assert ("Income", HUB_INCOME) in by, (
        "Real category named 'Income' must not be silenced by the self-loop guard"
    )
    assert by[("Income", HUB_INCOME)] == pytest.approx(3000.0)

    # Hub → "Savings" real expense category: must NOT merge with synthetic bucket.
    assert (HUB_INCOME, "Savings") in by, (
        "Real category named 'Savings' must produce its own expense link"
    )
    assert by[(HUB_INCOME, "Savings")] == pytest.approx(500.0)

    # Hub → "Other" real expense category: must NOT merge with synthetic fold bucket.
    assert (HUB_INCOME, "Other") in by, (
        "Real category named 'Other' must produce its own expense link"
    )
    assert by[(HUB_INCOME, "Other")] == pytest.approx(200.0)

    # The synthetic savings bucket uses HUB_SAVINGS (income 3000 > expense 700).
    assert (HUB_INCOME, HUB_SAVINGS) in by, (
        "Synthetic savings link must use HUB_SAVINGS sentinel, not collide with 'Savings' category"
    )
    # Savings = 3000 income - (500 + 200) expense = 2300
    assert by[(HUB_INCOME, HUB_SAVINGS)] == pytest.approx(2300.0)


# ── Fix 8: Filter-driven empty branch ────────────────────────────────


@pytest.mark.asyncio
async def test_empty_when_all_income_excluded_by_filter(session_factory):
    """Empty links when all income txns are excluded by reportable_transaction_filter.

    Seeds:
    - 1 transfer-leg income (linked_transaction_id IS NOT NULL) → excluded.
    - 1 manual-adjustment income (is_manual_adjustment=True) → excluded.
    - 1 real settled expense.

    Since no reportable income survives the filter, links must be [] and
    row_count == 0.  This exercises the filter-driven empty branch, not the
    row-absence empty branch.
    """
    async with session_factory() as db:
        org = Organization(name="Filter Empty Org", billing_cycle_day=1)
        db.add(org)
        await db.commit()

        at = AccountType(org_id=org.id, name="Checking")
        db.add(at)
        await db.commit()

        acct = Account(
            org_id=org.id, account_type_id=at.id,
            name="Bank", currency="EUR", balance=Decimal("0"),
        )
        db.add(acct)
        await db.commit()

        cat_inc = Category(org_id=org.id, name="Salary")
        cat_exp = Category(org_id=org.id, name="Rent")
        db.add_all([cat_inc, cat_exp])
        await db.commit()

        today = date(2026, 6, 1)

        # Transfer leg income — excluded by reportable_transaction_filter.
        tx_transfer_income = Transaction(
            org_id=org.id, account_id=acct.id, category_id=cat_inc.id,
            description="Transfer in", amount=Decimal("2000"),
            type=TransactionType.INCOME, status=TransactionStatus.SETTLED,
            date=today, settled_date=today,
        )
        tx_transfer_expense = Transaction(
            org_id=org.id, account_id=acct.id, category_id=cat_exp.id,
            description="Transfer out", amount=Decimal("2000"),
            type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
            date=today, settled_date=today,
        )
        db.add_all([tx_transfer_income, tx_transfer_expense])
        await db.flush()
        tx_transfer_income.linked_transaction_id = tx_transfer_expense.id
        tx_transfer_expense.linked_transaction_id = tx_transfer_income.id
        await db.flush()

        # Manual adjustment income — excluded by reportable_transaction_filter.
        db.add(
            Transaction(
                org_id=org.id, account_id=acct.id, category_id=cat_inc.id,
                description="Manual adj", amount=Decimal("500"),
                type=TransactionType.INCOME, status=TransactionStatus.SETTLED,
                date=today, settled_date=today,
                is_manual_adjustment=True,
            )
        )
        # A real settled expense.
        db.add(
            Transaction(
                org_id=org.id, account_id=acct.id, category_id=cat_exp.id,
                description="Rent", amount=Decimal("800"),
                type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
                date=today, settled_date=today,
            )
        )
        await db.commit()

        response = await build_sankey(db, org_id=org.id, query=SankeyQuery(filters=[]))

    assert response.links == [], (
        "All income excluded by filter → empty links (filter-driven empty, not row-absence)"
    )
    assert response.meta.row_count == 0
