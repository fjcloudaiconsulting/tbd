"""Pending-row settled_date semantics (Punch-list Item 13).

PR #163 enforced SETTLED ⇒ settled_date set. This file exercises the new
contract that PENDING rows MAY carry a non-null settled_date as the
"expected settlement date" used by ``effective_period_date_expr`` for
period bucketing in forecasts/filters.

Coverage:
- Create with status=pending and settled_date persists the date.
- Create with status=settled and explicit settled_date persists it
  (caller-supplied wins over the date fallback).
- Create with status=pending and no settled_date keeps it None.
- Create rejects settled_date < date at the schema layer.
- Update a pending row to set settled_date later.
- Update rejects settled_date < date.
- Update flipping settled→pending without an explicit settled_date
  clears the actual settled_date (we don't want to leak the historical
  actual into the "expected" slot).
"""
import datetime as dt
from decimal import Decimal

import pytest
import pytest_asyncio
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization, Transaction
from app.models.base import Base
from app.models.category import CategoryType
from app.models.transaction import TransactionStatus, TransactionType
from app.schemas.transaction import TransactionCreate, TransactionUpdate
from app.services import transaction_service
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


async def _seed_org_with_account(session: AsyncSession):
    org = Organization(name="T", billing_cycle_day=1)
    session.add(org)
    await session.flush()
    at = AccountType(org_id=org.id, name="CC", slug="credit_card", is_system=True)
    session.add(at)
    await session.flush()
    acct = Account(
        org_id=org.id, name="CC", account_type_id=at.id,
        balance=Decimal("0"), currency="EUR",
    )
    session.add(acct)
    cat = Category(org_id=org.id, name="Shopping", slug="shopping", type=CategoryType.EXPENSE, is_system=False)
    session.add(cat)
    await session.commit()
    return org, acct, cat


async def test_create_pending_with_settled_date_persists(db_session):
    org, acct, cat = await _seed_org_with_account(db_session)
    body = TransactionCreate(
        account_id=acct.id, category_id=cat.id,
        description="Pending CC charge", amount=Decimal("42.00"),
        type="expense", status="pending",
        date=dt.date(2026, 5, 1), settled_date=dt.date(2026, 6, 15),
    )
    tx = await transaction_service.create_transaction(db_session, org.id, body)
    assert tx.status == TransactionStatus.PENDING
    assert tx.settled_date == dt.date(2026, 6, 15)
    # Pending row → balance NOT applied.
    await db_session.refresh(acct)
    assert acct.balance == Decimal("0")


async def test_create_settled_with_explicit_settled_date(db_session):
    """Caller-supplied settled_date on SETTLED creates wins over the date fallback."""
    org, acct, cat = await _seed_org_with_account(db_session)
    body = TransactionCreate(
        account_id=acct.id, category_id=cat.id,
        description="Settled with explicit date", amount=Decimal("10.00"),
        type="expense", status="settled",
        date=dt.date(2026, 5, 1), settled_date=dt.date(2026, 5, 3),
    )
    tx = await transaction_service.create_transaction(db_session, org.id, body)
    assert tx.settled_date == dt.date(2026, 5, 3)


async def test_create_pending_without_settled_date_remains_none(db_session):
    org, acct, cat = await _seed_org_with_account(db_session)
    body = TransactionCreate(
        account_id=acct.id, category_id=cat.id,
        description="No expectation", amount=Decimal("5.00"),
        type="expense", status="pending",
        date=dt.date(2026, 5, 1),
    )
    tx = await transaction_service.create_transaction(db_session, org.id, body)
    assert tx.settled_date is None


def test_create_rejects_settled_date_before_date():
    """Schema-level guard: settled_date earlier than date is invalid."""
    with pytest.raises(PydanticValidationError):
        TransactionCreate(
            account_id=1, category_id=1,
            description="bad", amount=Decimal("1.00"),
            type="expense", status="pending",
            date=dt.date(2026, 5, 10),
            settled_date=dt.date(2026, 5, 1),
        )


async def test_update_pending_row_set_settled_date(db_session):
    """Setting settled_date on a previously-pending row persists the value."""
    org, acct, cat = await _seed_org_with_account(db_session)
    body = TransactionCreate(
        account_id=acct.id, category_id=cat.id,
        description="Pending", amount=Decimal("20.00"),
        type="expense", status="pending",
        date=dt.date(2026, 5, 1),
    )
    tx = await transaction_service.create_transaction(db_session, org.id, body)
    assert tx.settled_date is None

    update = TransactionUpdate(settled_date=dt.date(2026, 6, 1))
    result = await transaction_service.update_transaction(
        db_session, org.id, tx.id, update,
    )
    assert result.status == TransactionStatus.PENDING
    assert result.settled_date == dt.date(2026, 6, 1)


async def test_update_rejects_settled_date_before_date(db_session):
    """Service-layer guard against expected-settle < transaction-date."""
    org, acct, cat = await _seed_org_with_account(db_session)
    body = TransactionCreate(
        account_id=acct.id, category_id=cat.id,
        description="Pending", amount=Decimal("20.00"),
        type="expense", status="pending",
        date=dt.date(2026, 5, 10),
    )
    tx = await transaction_service.create_transaction(db_session, org.id, body)

    update = TransactionUpdate(settled_date=dt.date(2026, 5, 1))
    with pytest.raises(ValidationError):
        await transaction_service.update_transaction(
            db_session, org.id, tx.id, update,
        )


async def test_update_settled_to_pending_clears_settled_date_without_explicit(db_session):
    """Flipping settled→pending without supplying a new settled_date clears the
    historical actual; we don't repurpose a past actual as a future expectation.
    """
    org, acct, cat = await _seed_org_with_account(db_session)
    body = TransactionCreate(
        account_id=acct.id, category_id=cat.id,
        description="Was settled", amount=Decimal("30.00"),
        type="expense", status="settled",
        date=dt.date(2026, 5, 1),
    )
    tx = await transaction_service.create_transaction(db_session, org.id, body)
    assert tx.settled_date == dt.date(2026, 5, 1)

    # Flip without providing a new settled_date.
    update = TransactionUpdate(status="pending")
    result = await transaction_service.update_transaction(
        db_session, org.id, tx.id, update,
    )
    assert result.status == TransactionStatus.PENDING
    assert result.settled_date is None


async def test_update_settled_to_pending_with_explicit_settled_date_keeps_it(db_session):
    """If the caller flips to pending AND supplies a new settled_date, treat
    the value as the new expected-settlement date (don't drop it on the floor).
    """
    org, acct, cat = await _seed_org_with_account(db_session)
    body = TransactionCreate(
        account_id=acct.id, category_id=cat.id,
        description="Was settled", amount=Decimal("30.00"),
        type="expense", status="settled",
        date=dt.date(2026, 5, 1),
    )
    tx = await transaction_service.create_transaction(db_session, org.id, body)

    update = TransactionUpdate(status="pending", settled_date=dt.date(2026, 6, 1))
    result = await transaction_service.update_transaction(
        db_session, org.id, tx.id, update,
    )
    assert result.status == TransactionStatus.PENDING
    assert result.settled_date == dt.date(2026, 6, 1)
