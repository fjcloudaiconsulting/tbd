"""Tests for cc_statement_service (CC Statement Alerts V1, Task 3).

Proves the org-batched ledger loader + ``statement_outstanding`` helper
compute the SAME as-of-close owed amount the forecast bills (Slice 3's
``synthesize_account_cc_payments`` / ``cc_forecast_service.balance_at_close``)
-- no drift between what the forecast projects and what the close-day
alert (Task 9) reports. Activity after the close date must never inflate
the alerted amount (grace-period correctness).
"""
import datetime
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization, Transaction
from app.models.base import Base
from app.models.category import CategoryType
from app.models.transaction import TransactionStatus, TransactionType
from app.services import cc_statement_service as css


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


CLOSE_DATE = datetime.date(2026, 5, 25)
BEFORE_CLOSE = datetime.date(2026, 5, 10)
AFTER_CLOSE = datetime.date(2026, 6, 1)


async def _seed_cc(db: AsyncSession, *, opening_balance=Decimal("0.00")):
    """Minimal org + checking source + credit_card account, matching the
    fixture idiom in test_account_balance_forecast_service.py's _seed_cc."""
    org = Organization(name="Test", billing_cycle_day=1)
    db.add(org)
    await db.flush()

    cc_type = AccountType(org_id=org.id, name="Credit Card", slug="credit_card", is_system=True)
    source_type = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db.add_all([cc_type, source_type])
    await db.flush()

    source = Account(
        org_id=org.id, name="Checking", account_type_id=source_type.id,
        balance=Decimal("1000.00"), currency="EUR", is_default=True,
    )
    db.add(source)
    await db.flush()

    cc = Account(
        org_id=org.id, name="Visa", account_type_id=cc_type.id,
        balance=Decimal("0.00"), currency="EUR", is_default=False,
        close_day=25, payment_day=1, payment_day_relative_month=1,
        payment_source_account_id=source.id, opening_balance=opening_balance,
    )
    db.add(cc)
    await db.flush()

    cat_expense = Category(org_id=org.id, name="Groceries", slug="groceries", type=CategoryType.EXPENSE)
    db.add(cat_expense)
    await db.flush()

    return {"org_id": org.id, "cc": cc, "source": source, "cat_expense": cat_expense.id}


def _charge(seed, *, amount, on, settled=True):
    return Transaction(
        org_id=seed["org_id"], account_id=seed["cc"].id, category_id=seed["cat_expense"],
        amount=Decimal(amount), type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED if settled else TransactionStatus.PENDING,
        date=on, settled_date=on if settled else None, description="x",
        is_imported=False, is_manual_adjustment=False,
    )


@pytest_asyncio.fixture
async def seed_cc_with_ledger(db_session):
    """CC with opening_balance 0; a -100 purchase eff BEFORE close; a -30
    purchase eff AFTER close. Only the before-close charge should count."""
    seed = await _seed_cc(db_session)
    cc = seed["cc"]
    db_session.add_all([
        _charge(seed, amount="100.00", on=BEFORE_CLOSE),
        _charge(seed, amount="30.00", on=AFTER_CLOSE),
    ])
    cc.balance = Decimal("-100.00")
    await db_session.commit()
    return cc, CLOSE_DATE


@pytest_asyncio.fixture
async def seed_cc_paid_off(db_session):
    seed = await _seed_cc(db_session)
    cc = seed["cc"]
    await db_session.commit()
    return cc, CLOSE_DATE


async def test_statement_outstanding_matches_as_of_close(db_session, seed_cc_with_ledger):
    acct, close_date = seed_cc_with_ledger
    owed = await css.statement_outstanding(
        db_session, org_id=acct.org_id, account=acct, close_date=close_date
    )
    assert owed == Decimal("100.00")   # the after-close 30 is excluded


async def test_zero_when_paid_off(db_session, seed_cc_paid_off):
    acct, close_date = seed_cc_paid_off
    owed = await css.statement_outstanding(
        db_session, org_id=acct.org_id, account=acct, close_date=close_date
    )
    assert owed == Decimal("0")


async def test_load_cc_ledgers_is_org_scoped(db_session):
    """A security-review checkpoint: a second org's CC ledger must never
    leak into another org's lookup, even when its account id is passed in
    the same batch."""
    seed_a = await _seed_cc(db_session)
    seed_b = await _seed_cc(db_session)
    db_session.add_all([
        _charge(seed_a, amount="50.00", on=BEFORE_CLOSE),
        _charge(seed_b, amount="999.00", on=BEFORE_CLOSE),
    ])
    await db_session.commit()

    ledgers = await css.load_cc_ledgers(
        db_session, seed_a["org_id"], [seed_a["cc"].id, seed_b["cc"].id], CLOSE_DATE
    )

    assert seed_b["cc"].id not in ledgers
    assert ledgers[seed_a["cc"].id] == [(BEFORE_CLOSE, Decimal("-50.00"))]
