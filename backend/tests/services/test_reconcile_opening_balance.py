"""reconcile_account must reconcile against the SAME invariant the live
balance is built on: ``balance == opening_balance + Σ settled(income − expense)``.

Before the fix, ``computed`` omitted ``opening_balance``, so every account
with a non-zero opening balance was falsely reported inconsistent (by exactly
the opening balance). Regression guard for the 2026-06-14 investigation.
"""
import pytest
import pytest_asyncio
from datetime import date
from decimal import Decimal

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models import Account, AccountType, Category, Organization
from app.models.category import CategoryType
from app.schemas.transaction import TransactionCreate
from app.services import transaction_service as ts


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )

    @event.listens_for(Engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor(); cur.execute("PRAGMA foreign_keys=ON"); cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed(db, *, opening: str):
    org = Organization(name="T", billing_cycle_day=1)
    db.add(org); await db.flush()
    at = AccountType(org_id=org.id, name="Bank", slug="bank", is_system=True)
    db.add(at); await db.flush()
    acct = Account(org_id=org.id, name="A", account_type_id=at.id,
                   balance=Decimal(opening), currency="EUR",
                   opening_balance=Decimal(opening), opening_balance_date=date(2026, 1, 1))
    db.add(acct)
    db.add(Category(org_id=org.id, name="G", slug="g", type=CategoryType.BOTH, is_system=True))
    await db.flush(); await db.commit()
    return org, acct


@pytest.mark.asyncio
async def test_reconcile_consistent_with_nonzero_opening_and_no_txns(db_session):
    db = db_session
    org, acct = await _seed(db, opening="1000.00")
    stored, computed, consistent = await ts.reconcile_account(db, org.id, acct)
    assert stored == Decimal("1000.00")
    assert computed == Decimal("1000.00")   # opening included
    assert consistent is True


@pytest.mark.asyncio
async def test_reconcile_consistent_opening_plus_settled_txns(db_session):
    db = db_session
    org, acct = await _seed(db, opening="1000.00")
    from sqlalchemy import select
    gid = await db.scalar(select(Category.id).where(Category.org_id == org.id))
    await ts.create_transaction(db, org.id, TransactionCreate(
        account_id=acct.id, category_id=gid, description="pay",
        amount=Decimal("200.00"), type="income", status="settled", date=date(2026, 6, 1)))
    await ts.create_transaction(db, org.id, TransactionCreate(
        account_id=acct.id, category_id=gid, description="buy",
        amount=Decimal("50.00"), type="expense", status="settled", date=date(2026, 6, 2)))
    await db.refresh(acct)
    stored, computed, consistent = await ts.reconcile_account(db, org.id, acct)
    # balance = 1000 + 200 - 50 = 1150; computed must equal it.
    assert stored == Decimal("1150.00")
    assert computed == Decimal("1150.00")
    assert consistent is True


async def _seed_two(db, *, opening_a: str, opening_b: str):
    """Seed an org with two accounts (nonzero openings) + a Transfer category."""
    org = Organization(name="T", billing_cycle_day=1)
    db.add(org); await db.flush()
    at = AccountType(org_id=org.id, name="Bank", slug="bank", is_system=True)
    db.add(at); await db.flush()
    acct_a = Account(org_id=org.id, name="A", account_type_id=at.id,
                     balance=Decimal(opening_a), currency="EUR",
                     opening_balance=Decimal(opening_a),
                     opening_balance_date=date(2026, 1, 1))
    acct_b = Account(org_id=org.id, name="B", account_type_id=at.id,
                     balance=Decimal(opening_b), currency="EUR",
                     opening_balance=Decimal(opening_b),
                     opening_balance_date=date(2026, 1, 1))
    db.add(acct_a); db.add(acct_b)
    db.add(Category(org_id=org.id, name="Transfer", slug="transfer",
                    type=CategoryType.BOTH, is_system=True))
    await db.flush(); await db.commit()
    return org, acct_a, acct_b


@pytest.mark.asyncio
async def test_reconcile_consistent_with_transfer_legs(db_session):
    """A transfer creates settled EXPENSE+INCOME legs counted by reconcile.
    Both source and destination accounts must stay consistent: each balance
    moved by the transfer amount, and computed includes the matching leg."""
    db = db_session
    org, acct_a, acct_b = await _seed_two(db, opening_a="1000.00", opening_b="400.00")

    from app.schemas.transaction import TransferCreate
    await ts.create_transfer(db, org.id, TransferCreate(
        from_account_id=acct_a.id, to_account_id=acct_b.id,
        amount=Decimal("250.00"), status="settled", date=date(2026, 6, 1)))
    await db.refresh(acct_a)
    await db.refresh(acct_b)

    # Source: 1000 - 250 = 750 (expense leg). Dest: 400 + 250 = 650 (income leg).
    s_a, c_a, ok_a = await ts.reconcile_account(db, org.id, acct_a)
    assert s_a == Decimal("750.00")
    assert c_a == Decimal("750.00")
    assert ok_a is True

    s_b, c_b, ok_b = await ts.reconcile_account(db, org.id, acct_b)
    assert s_b == Decimal("650.00")
    assert c_b == Decimal("650.00")
    assert ok_b is True


@pytest.mark.asyncio
async def test_reconcile_consistent_after_negative_opening_delta(db_session):
    """Lowering opening_balance (1000 -> 300) must shift balance down by 700 and
    keep reconcile consistent. Mirrors the router's opening-shift path applied
    to a freshly-seeded account (no txns: balance tracks opening directly)."""
    db = db_session
    org, acct = await _seed(db, opening="1000.00")
    assert acct.balance == Decimal("1000.00")

    # Apply the same shift the router's _apply_non_type_fields performs.
    new_opening = Decimal("300.00")
    acct.balance += new_opening - acct.opening_balance
    acct.opening_balance = new_opening
    await db.commit()
    await db.refresh(acct)

    assert acct.balance == Decimal("300.00")  # shifted down by 700
    stored, computed, consistent = await ts.reconcile_account(db, org.id, acct)
    assert stored == Decimal("300.00")
    assert computed == Decimal("300.00")
    assert consistent is True
