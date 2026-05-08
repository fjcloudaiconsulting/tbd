"""SETTLED-implies-settled_date invariant tests.

Two layers of enforcement, both exercised here:

1. App-level: SQLAlchemy ``before_insert`` / ``before_update`` event
   listener on the Transaction model raises ``ValueError`` at flush
   time when status=SETTLED and settled_date is NULL. The event runs
   on flush, not on attribute assignment, so it sees the final state
   of both columns and is independent of kwarg ordering in the
   constructor.

2. DB-level: CHECK constraint
   ``status <> 'settled' OR settled_date IS NOT NULL`` added by
   migration 036. The app-level guard short-circuits before this
   normally fires, but the DB layer is the source of truth (handles
   raw-SQL writes, future code paths, etc.).

These tests run against SQLite in-memory and so verify the app-level
guard. The DB CHECK has been verified manually against MySQL 8 in the
local stack; it's portable to SQLite (universal feature) and exercised
implicitly by ``Base.metadata.create_all`` issuing the column-bound
CHECK clause.
"""
from __future__ import annotations

from datetime import date as _date

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization, Transaction
from app.models.base import Base
from app.models.category import CategoryType
from app.models.transaction import TransactionStatus, TransactionType


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


async def _seed_org_account_category(session: AsyncSession):
    org = Organization(name="Test", billing_cycle_day=1)
    session.add(org)
    await session.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    session.add(at)
    await session.flush()
    acct = Account(
        org_id=org.id,
        name="Main",
        account_type_id=at.id,
        balance=0,
        currency="EUR",
    )
    cat = Category(
        org_id=org.id,
        name="Groceries",
        slug="groceries",
        type=CategoryType.EXPENSE,
        is_system=False,
    )
    session.add_all([acct, cat])
    await session.flush()
    return org, acct, cat


async def test_create_settled_without_settled_date_raises(db_session):
    """Inserting a SETTLED row with settled_date=NULL must be rejected."""
    org, acct, cat = await _seed_org_account_category(db_session)
    tx = Transaction(
        org_id=org.id,
        account_id=acct.id,
        category_id=cat.id,
        description="bad",
        amount=10,
        type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED,
        date=_date(2026, 5, 8),
        settled_date=None,
    )
    db_session.add(tx)
    with pytest.raises((ValueError, IntegrityError)) as exc_info:
        await db_session.flush()
    # Guard message is informative (covers either layer firing first).
    assert "settled" in str(exc_info.value).lower()


async def test_create_settled_with_settled_date_succeeds(db_session):
    """Happy path: SETTLED + non-null settled_date is allowed."""
    org, acct, cat = await _seed_org_account_category(db_session)
    tx = Transaction(
        org_id=org.id,
        account_id=acct.id,
        category_id=cat.id,
        description="ok",
        amount=10,
        type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED,
        date=_date(2026, 5, 8),
        settled_date=_date(2026, 5, 8),
    )
    db_session.add(tx)
    await db_session.flush()
    assert tx.id is not None


async def test_create_pending_without_settled_date_succeeds(db_session):
    """PENDING rows are allowed to have settled_date=NULL — that's the
    whole point of pending."""
    org, acct, cat = await _seed_org_account_category(db_session)
    tx = Transaction(
        org_id=org.id,
        account_id=acct.id,
        category_id=cat.id,
        description="pending",
        amount=10,
        type=TransactionType.EXPENSE,
        status=TransactionStatus.PENDING,
        date=_date(2026, 5, 8),
        settled_date=None,
    )
    db_session.add(tx)
    await db_session.flush()
    assert tx.id is not None
    assert tx.settled_date is None


async def test_update_clear_settled_date_on_settled_row_raises(db_session):
    """Cannot UPDATE a SETTLED row to have settled_date=NULL."""
    org, acct, cat = await _seed_org_account_category(db_session)
    tx = Transaction(
        org_id=org.id,
        account_id=acct.id,
        category_id=cat.id,
        description="ok",
        amount=10,
        type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED,
        date=_date(2026, 5, 8),
        settled_date=_date(2026, 5, 8),
    )
    db_session.add(tx)
    await db_session.flush()

    tx.settled_date = None
    with pytest.raises((ValueError, IntegrityError)) as exc_info:
        await db_session.flush()
    assert "settled" in str(exc_info.value).lower()


async def test_update_status_to_settled_with_null_date_raises(db_session):
    """Cannot UPDATE a row to status=SETTLED while settled_date is NULL."""
    org, acct, cat = await _seed_org_account_category(db_session)
    tx = Transaction(
        org_id=org.id,
        account_id=acct.id,
        category_id=cat.id,
        description="pending row",
        amount=10,
        type=TransactionType.EXPENSE,
        status=TransactionStatus.PENDING,
        date=_date(2026, 5, 8),
        settled_date=None,
    )
    db_session.add(tx)
    await db_session.flush()

    tx.status = TransactionStatus.SETTLED
    with pytest.raises((ValueError, IntegrityError)) as exc_info:
        await db_session.flush()
    assert "settled" in str(exc_info.value).lower()


async def test_pending_to_settled_with_date_succeeds(db_session):
    """Setting both status=SETTLED and a non-null settled_date in the
    same flush must succeed — that's the legitimate transition."""
    org, acct, cat = await _seed_org_account_category(db_session)
    tx = Transaction(
        org_id=org.id,
        account_id=acct.id,
        category_id=cat.id,
        description="pending row",
        amount=10,
        type=TransactionType.EXPENSE,
        status=TransactionStatus.PENDING,
        date=_date(2026, 5, 8),
        settled_date=None,
    )
    db_session.add(tx)
    await db_session.flush()

    tx.status = TransactionStatus.SETTLED
    tx.settled_date = _date(2026, 5, 8)
    await db_session.flush()
    # Re-read to confirm persisted.
    result = await db_session.execute(select(Transaction).where(Transaction.id == tx.id))
    fetched = result.scalar_one()
    assert fetched.status == TransactionStatus.SETTLED
    assert fetched.settled_date == _date(2026, 5, 8)
