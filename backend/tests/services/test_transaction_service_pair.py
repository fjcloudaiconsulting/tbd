"""Pairing primitives for transfer-between-accounts repair toolkit."""
import pytest
import pytest_asyncio
from datetime import date
from decimal import Decimal

from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models import Account, AccountType, Category, Organization, Transaction
from app.models.category import CategoryType
from app.models.transaction import TransactionStatus, TransactionType
from app.schemas.transaction import TransferCreate
from app.services import transaction_service


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


async def test_create_transfer_calls_link_pair_and_links_bidirectionally(db_session):
    """Pins create_transfer's externally-observable behavior before and after refactor:
    two paired rows, bidirectional linked_transaction_id, equal amounts, opposite types,
    balances correctly mutated."""
    org = Organization(name="Test", billing_cycle_day=1)
    db_session.add(org)
    await db_session.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db_session.add(at)
    await db_session.flush()
    src = Account(org_id=org.id, name="Src", account_type_id=at.id, balance=Decimal("100"), currency="EUR")
    dst = Account(org_id=org.id, name="Dst", account_type_id=at.id, balance=Decimal("0"), currency="EUR")
    db_session.add_all([src, dst])
    cat = Category(org_id=org.id, name="Transfer", slug="transfer", type=CategoryType.BOTH, is_system=True)
    db_session.add(cat)
    await db_session.flush()

    body = TransferCreate(
        from_account_id=src.id, to_account_id=dst.id,
        amount=Decimal("25"), date=date(2026, 5, 1), status="settled",
    )
    expense_tx, income_tx = await transaction_service.create_transfer(db_session, org.id, body)

    assert expense_tx.linked_transaction_id == income_tx.id
    assert income_tx.linked_transaction_id == expense_tx.id
    assert expense_tx.type == TransactionType.EXPENSE
    assert income_tx.type == TransactionType.INCOME
    assert expense_tx.amount == income_tx.amount

    # Refresh accounts to verify balance updates
    await db_session.refresh(src)
    await db_session.refresh(dst)
    assert src.balance == Decimal("75")
    assert dst.balance == Decimal("25")


async def test_create_transaction_no_commit_does_not_commit(db_session):
    """The internal primitive must flush but not commit, so callers can wrap
    it in their own transaction. Verified by inspecting that a rollback after
    the call removes the inserted row entirely.
    """
    from app.services.transaction_service import _create_transaction_no_commit
    from app.schemas.transaction import TransactionCreate

    org = Organization(name="Test", billing_cycle_day=1)
    db_session.add(org)
    await db_session.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db_session.add(at)
    await db_session.flush()
    acct = Account(org_id=org.id, name="A", account_type_id=at.id, balance=Decimal("0"), currency="EUR")
    db_session.add(acct)
    cat = Category(org_id=org.id, name="C", slug="c", type=CategoryType.BOTH, is_system=True)
    db_session.add(cat)
    await db_session.flush()

    body = TransactionCreate(
        account_id=acct.id, category_id=cat.id, description="x",
        amount=Decimal("5"), type="expense", status="settled", date=date(2026, 5, 1),
    )
    tx = await _create_transaction_no_commit(db_session, org.id, body)
    assert tx.id is not None  # flushed, has an id
    tx_id = tx.id

    # Roll back to confirm the primitive did not commit
    await db_session.rollback()

    # The row should be gone
    result = await db_session.execute(
        select(Transaction).where(Transaction.id == tx_id)
    )
    assert result.scalar_one_or_none() is None


async def test_find_match_candidates_returns_un_linked_opposite_type_within_window(db_session):
    """Same currency, opposite type, equal amount, ±3 days, settled, non-recurring."""
    org = Organization(name="T", billing_cycle_day=1)
    db_session.add(org)
    await db_session.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db_session.add(at)
    await db_session.flush()
    acct_a = Account(org_id=org.id, name="A", account_type_id=at.id, balance=Decimal("0"), currency="EUR")
    acct_b = Account(org_id=org.id, name="B", account_type_id=at.id, balance=Decimal("0"), currency="EUR")
    db_session.add_all([acct_a, acct_b])
    cat = Category(org_id=org.id, name="C", slug="c", type=CategoryType.BOTH, is_system=True)
    db_session.add(cat)
    await db_session.flush()
    tx_b = Transaction(
        org_id=org.id, account_id=acct_a.id, category_id=cat.id,
        description="src", amount=Decimal("100"),
        type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
        date=date(2026, 5, 1), settled_date=date(2026, 5, 1),
    )
    db_session.add(tx_b)
    await db_session.commit()

    candidates = await transaction_service.find_match_candidates(
        db_session, org.id,
        source_type=TransactionType.INCOME,
        amount=Decimal("100"),
        account_id_excluded=acct_b.id,
        date=date(2026, 5, 2),
        currency="EUR",
    )
    assert len(candidates) == 1
    assert candidates[0].id == tx_b.id


async def test_find_match_candidates_excludes_already_linked_rows(db_session):
    """Linked rows must not appear as candidates."""
    from tests.services.test_transaction_filters import _seed_pair
    expense, income = await _seed_pair(db_session)
    candidates = await transaction_service.find_match_candidates(
        db_session, expense.org_id,
        source_type=TransactionType.INCOME,
        amount=expense.amount,
        account_id_excluded=income.account_id,
        date=expense.date,
        currency="EUR",
    )
    assert candidates == []


async def test_find_match_candidates_excludes_pending_rows(db_session):
    """Pending rows are not eligible matches."""
    org = Organization(name="T", billing_cycle_day=1)
    db_session.add(org)
    await db_session.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db_session.add(at)
    await db_session.flush()
    acct_a = Account(org_id=org.id, name="A", account_type_id=at.id, balance=Decimal("0"), currency="EUR")
    acct_b = Account(org_id=org.id, name="B", account_type_id=at.id, balance=Decimal("0"), currency="EUR")
    db_session.add_all([acct_a, acct_b])
    cat = Category(org_id=org.id, name="C", slug="c", type=CategoryType.BOTH, is_system=True)
    db_session.add(cat)
    await db_session.flush()
    pending = Transaction(
        org_id=org.id, account_id=acct_a.id, category_id=cat.id,
        description="x", amount=Decimal("50"),
        type=TransactionType.EXPENSE, status=TransactionStatus.PENDING,
        date=date(2026, 5, 1),
    )
    db_session.add(pending)
    await db_session.commit()
    candidates = await transaction_service.find_match_candidates(
        db_session, org.id,
        source_type=TransactionType.INCOME,
        amount=Decimal("50"),
        account_id_excluded=acct_b.id,
        date=date(2026, 5, 1),
        currency="EUR",
    )
    assert candidates == []


async def test_find_match_candidates_filters_by_currency(db_session):
    """Different-currency accounts must not produce matches."""
    org = Organization(name="T", billing_cycle_day=1)
    db_session.add(org)
    await db_session.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db_session.add(at)
    await db_session.flush()
    acct_eur = Account(org_id=org.id, name="EUR", account_type_id=at.id, balance=Decimal("0"), currency="EUR")
    acct_usd = Account(org_id=org.id, name="USD", account_type_id=at.id, balance=Decimal("0"), currency="USD")
    db_session.add_all([acct_eur, acct_usd])
    cat = Category(org_id=org.id, name="C", slug="c", type=CategoryType.BOTH, is_system=True)
    db_session.add(cat)
    await db_session.flush()
    usd_expense = Transaction(
        org_id=org.id, account_id=acct_usd.id, category_id=cat.id,
        description="x", amount=Decimal("100"),
        type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
        date=date(2026, 5, 1), settled_date=date(2026, 5, 1),
    )
    db_session.add(usd_expense)
    await db_session.commit()
    candidates = await transaction_service.find_match_candidates(
        db_session, org.id,
        source_type=TransactionType.INCOME,
        amount=Decimal("100"),
        account_id_excluded=acct_eur.id,
        date=date(2026, 5, 1),
        currency="EUR",
    )
    assert candidates == []


async def test_find_match_candidates_skips_recurring(db_session):
    """Rows with recurring_id IS NOT NULL are skipped."""
    from app.models.recurring import RecurringTransaction, Frequency
    org = Organization(name="T", billing_cycle_day=1)
    db_session.add(org)
    await db_session.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db_session.add(at)
    await db_session.flush()
    acct_a = Account(org_id=org.id, name="A", account_type_id=at.id, balance=Decimal("0"), currency="EUR")
    acct_b = Account(org_id=org.id, name="B", account_type_id=at.id, balance=Decimal("0"), currency="EUR")
    db_session.add_all([acct_a, acct_b])
    cat = Category(org_id=org.id, name="C", slug="c", type=CategoryType.BOTH, is_system=True)
    db_session.add(cat)
    await db_session.flush()
    rec = RecurringTransaction(
        org_id=org.id, account_id=acct_a.id, category_id=cat.id,
        description="rent", amount=Decimal("100"), type="expense",
        frequency=Frequency.MONTHLY, next_due_date=date(2026, 1, 1),
    )
    db_session.add(rec)
    await db_session.flush()
    tx = Transaction(
        org_id=org.id, account_id=acct_a.id, category_id=cat.id,
        description="rent", amount=Decimal("100"),
        type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
        date=date(2026, 5, 1), settled_date=date(2026, 5, 1),
        recurring_id=rec.id,
    )
    db_session.add(tx)
    await db_session.commit()
    candidates = await transaction_service.find_match_candidates(
        db_session, org.id,
        source_type=TransactionType.INCOME,
        amount=Decimal("100"),
        account_id_excluded=acct_b.id,
        date=date(2026, 5, 1),
        currency="EUR",
    )
    assert candidates == []


async def test_find_match_candidates_orders_by_date_proximity_then_id(db_session):
    """Closest by date diff first, then by id."""
    org = Organization(name="T", billing_cycle_day=1)
    db_session.add(org)
    await db_session.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db_session.add(at)
    await db_session.flush()
    acct_a = Account(org_id=org.id, name="A", account_type_id=at.id, balance=Decimal("0"), currency="EUR")
    acct_b = Account(org_id=org.id, name="B", account_type_id=at.id, balance=Decimal("0"), currency="EUR")
    db_session.add_all([acct_a, acct_b])
    cat = Category(org_id=org.id, name="C", slug="c", type=CategoryType.BOTH, is_system=True)
    db_session.add(cat)
    await db_session.flush()
    # Three eligible rows: -2d, +1d, +1d (same date, different ids)
    rows = [
        Transaction(
            org_id=org.id, account_id=acct_a.id, category_id=cat.id,
            description=f"row{i}", amount=Decimal("100"),
            type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
            date=d, settled_date=d,
        )
        for i, d in enumerate([date(2026, 4, 29), date(2026, 5, 2), date(2026, 5, 2)])
    ]
    db_session.add_all(rows)
    await db_session.commit()
    # Query date 2026-05-01: distances are 2, 1, 1
    candidates = await transaction_service.find_match_candidates(
        db_session, org.id,
        source_type=TransactionType.INCOME,
        amount=Decimal("100"),
        account_id_excluded=acct_b.id,
        date=date(2026, 5, 1),
        currency="EUR",
    )
    assert len(candidates) == 3
    # Closest first: the two +1d rows (sorted by id ASC), then -2d
    assert candidates[0].date == date(2026, 5, 2)
    assert candidates[1].date == date(2026, 5, 2)
    assert candidates[2].date == date(2026, 4, 29)
    assert candidates[0].id < candidates[1].id
