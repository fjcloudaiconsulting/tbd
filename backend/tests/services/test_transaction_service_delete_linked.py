"""Regression: deleting a transfer pair must not trip SQLAlchemy's
topological-sort cycle detection.

Both halves of a transfer carry ``linked_transaction_id`` pointing at
each other. Without ``post_update=True`` on the
``Transaction.linked_transaction`` relationship, the ORM unit of work
cannot pick an order for the two DELETEs and raises
``CircularDependencyError`` before any SQL is emitted.
"""
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


async def _seed_pair(session: AsyncSession):
    """Seed a linked transfer pair: one EXPENSE leg, one INCOME leg, mutual FK."""
    org = Organization(name="T", billing_cycle_day=1)
    session.add(org)
    await session.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    session.add(at)
    await session.flush()
    src = Account(
        org_id=org.id, name="Src", account_type_id=at.id,
        balance=Decimal("400"), currency="EUR",
    )
    dst = Account(
        org_id=org.id, name="Dst", account_type_id=at.id,
        balance=Decimal("100"), currency="EUR",
    )
    session.add_all([src, dst])
    cat = Category(
        org_id=org.id, name="Transfer", slug="transfer",
        type=CategoryType.BOTH, is_system=True,
    )
    session.add(cat)
    await session.flush()
    expense = Transaction(
        org_id=org.id, account_id=src.id, category_id=cat.id,
        description="t", amount=Decimal("100"),
        type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
        date=date(2026, 5, 1), settled_date=date(2026, 5, 1),
    )
    income = Transaction(
        org_id=org.id, account_id=dst.id, category_id=cat.id,
        description="t", amount=Decimal("100"),
        type=TransactionType.INCOME, status=TransactionStatus.SETTLED,
        date=date(2026, 5, 1), settled_date=date(2026, 5, 1),
    )
    session.add_all([expense, income])
    await session.flush()
    expense.linked_transaction_id = income.id
    income.linked_transaction_id = expense.id
    await session.commit()
    return org, src, dst, expense, income


async def test_delete_transaction_on_transfer_pair_no_circular_dependency(db_session):
    """delete_transaction on one half of a transfer pair removes both halves
    without raising CircularDependencyError."""
    org, src, dst, expense, income = await _seed_pair(db_session)

    await transaction_service.delete_transaction(db_session, org.id, expense.id)

    remaining = await db_session.scalars(
        select(Transaction).where(Transaction.id.in_([expense.id, income.id]))
    )
    assert remaining.all() == []


async def test_bulk_delete_transactions_on_transfer_pair_no_circular_dependency(db_session):
    """bulk_delete_transactions on both halves of a transfer pair removes
    both without raising CircularDependencyError, even if the caller's
    per-row FK null-out were removed."""
    org, src, dst, expense, income = await _seed_pair(db_session)

    deleted, skipped = await transaction_service.bulk_delete_transactions(
        db_session, org.id, [expense.id, income.id]
    )

    assert deleted == 2
    assert skipped == []
    remaining = await db_session.scalars(
        select(Transaction).where(Transaction.id.in_([expense.id, income.id]))
    )
    assert remaining.all() == []
