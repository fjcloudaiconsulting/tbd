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
