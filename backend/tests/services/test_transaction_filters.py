"""Helpers that express transfer-leg exclusion in aggregates and predicates."""
import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models import Account, AccountType, Category, Organization, Transaction
from app.models.category import CategoryType
from app.models.transaction import TransactionStatus, TransactionType
from app.services.transaction_filters import (
    is_reportable_transaction,
    is_transfer_leg,
    non_reverted_transaction_filter,
    reportable_transaction_filter,
)


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


def _seed_org(session):
    org = Organization(name="Test", billing_cycle_day=1)
    session.add(org)
    return org


async def _seed_pair(session: AsyncSession):
    """Helper used by this test file AND imported by sibling test files in PR-B.
    Seeds an org, two accounts with currency=EUR, a Transfer category, and one
    SETTLED EXPENSE+INCOME pair linked bidirectionally. Returns (expense, income).
    """
    from datetime import date as _date
    org = _seed_org(session)
    await session.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    session.add(at)
    await session.flush()
    src = Account(org_id=org.id, name="Src", account_type_id=at.id, balance=0, currency="EUR")
    dst = Account(org_id=org.id, name="Dst", account_type_id=at.id, balance=0, currency="EUR")
    session.add_all([src, dst])
    await session.flush()
    cat = Category(org_id=org.id, name="Transfer", slug="transfer", type=CategoryType.BOTH, is_system=True)
    session.add(cat)
    await session.flush()

    expense = Transaction(
        org_id=org.id, account_id=src.id, category_id=cat.id,
        description="t", amount=10, type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED, date=_date(2026, 5, 1), settled_date=_date(2026, 5, 1),
    )
    income = Transaction(
        org_id=org.id, account_id=dst.id, category_id=cat.id,
        description="t", amount=10, type=TransactionType.INCOME,
        status=TransactionStatus.SETTLED, date=_date(2026, 5, 1), settled_date=_date(2026, 5, 1),
    )
    session.add_all([expense, income])
    await session.flush()
    expense.linked_transaction_id = income.id
    income.linked_transaction_id = expense.id
    await session.commit()
    return expense, income


async def test_is_reportable_transaction_returns_true_for_unlinked(db_session):
    expense, _ = await _seed_pair(db_session)
    expense.linked_transaction_id = None
    assert is_reportable_transaction(expense) is True


async def test_is_reportable_transaction_returns_false_for_linked(db_session):
    expense, _ = await _seed_pair(db_session)
    assert is_reportable_transaction(expense) is False


async def test_is_transfer_leg_returns_true_for_linked(db_session):
    expense, _ = await _seed_pair(db_session)
    assert is_transfer_leg(expense) is True


async def test_is_transfer_leg_returns_false_for_unlinked(db_session):
    expense, _ = await _seed_pair(db_session)
    expense.linked_transaction_id = None
    assert is_transfer_leg(expense) is False


async def test_reportable_transaction_filter_excludes_transfer_legs_in_query(db_session):
    expense, income = await _seed_pair(db_session)
    result = await db_session.execute(
        select(Transaction).where(reportable_transaction_filter())
    )
    rows = list(result.scalars().all())
    assert rows == []  # both legs are linked


async def test_non_reverted_filter_keeps_transfers_but_drops_reverted(db_session):
    """``non_reverted_transaction_filter`` (the Reports "include transfers &
    adjustments" half) keeps transfer legs but still drops reverted
    reconciliation rows, whose amount was reverted from the account balance."""
    from datetime import date as _date

    expense, income = await _seed_pair(db_session)  # two linked (accepted) legs
    rej = Transaction(
        org_id=expense.org_id,
        account_id=expense.account_id,
        category_id=expense.category_id,
        description="rejected",
        amount=15,
        type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED,
        date=_date(2026, 5, 1),
        settled_date=_date(2026, 5, 1),
        reconciliation_state="rejected",
    )
    db_session.add(rej)
    await db_session.commit()

    result = await db_session.execute(
        select(Transaction).where(non_reverted_transaction_filter())
    )
    ids = {r.id for r in result.scalars().all()}
    assert expense.id in ids and income.id in ids  # transfer legs retained
    assert rej.id not in ids  # reverted row dropped
