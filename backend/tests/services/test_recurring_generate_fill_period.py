"""generate_due_transactions fills the current billing cycle window.

- future-in-period instances are materialized as PENDING
- auto_settle settles only instances whose date has passed (<= today)
- a settle-on-due sweep promotes previously-generated PENDING auto_settle rows
- overdue prior-period instances are still caught up
- re-running in the same period is idempotent (no duplicates)
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.category import CategoryType
from app.models.recurring import RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus
from app.services import recurring_service


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


async def _seed(db: AsyncSession, *, cycle_day: int = 1) -> dict:
    org = Organization(name="T", billing_cycle_day=cycle_day)
    db.add(org)
    await db.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db.add(at)
    await db.flush()
    acct = Account(
        org_id=org.id, name="Main", account_type_id=at.id,
        balance=Decimal("0"), currency="EUR",
    )
    db.add(acct)
    await db.flush()
    exp = Category(org_id=org.id, name="Rent", slug="rent", type=CategoryType.EXPENSE)
    inc = Category(org_id=org.id, name="Salary", slug="salary", type=CategoryType.INCOME)
    db.add_all([exp, inc])
    await db.commit()
    return {
        "org_id": org.id, "account_id": acct.id,
        "exp_cat": exp.id, "inc_cat": inc.id,
    }


async def _add_template(db, seed, *, type_, cat, amount, freq, next_due, auto_settle):
    r = RecurringTransaction(
        org_id=seed["org_id"], account_id=seed["account_id"], category_id=cat,
        description="t", amount=Decimal(amount), type=type_, frequency=freq,
        next_due_date=next_due, auto_settle=auto_settle, is_active=True,
    )
    db.add(r)
    await db.commit()
    return r


async def _txns(db, org_id):
    res = await db.execute(
        select(Transaction).where(Transaction.org_id == org_id).order_by(Transaction.date)
    )
    return list(res.scalars().all())


# today fixed mid-period; cycle_day=1 -> window June 1..30
TODAY = date(2026, 6, 15)


async def test_future_in_period_is_pending(db_session):
    seed = await _seed(db_session, cycle_day=1)
    await _add_template(db_session, seed, type_="expense", cat=seed["exp_cat"],
                        amount="500", freq="monthly", next_due=date(2026, 6, 25),
                        auto_settle=False)
    summary = await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=TODAY)
    txns = await _txns(db_session, seed["org_id"])
    assert len(txns) == 1
    assert txns[0].date == date(2026, 6, 25)
    assert txns[0].status == TransactionStatus.PENDING
    assert txns[0].settled_date is None
    assert summary["generated"] == 1
    assert summary["pending"] == 1
    assert summary["period_end"] == "2026-06-30"


async def test_auto_settle_past_is_settled_and_adjusts_balance(db_session):
    seed = await _seed(db_session, cycle_day=1)
    await _add_template(db_session, seed, type_="income", cat=seed["inc_cat"],
                        amount="1000", freq="monthly", next_due=date(2026, 6, 10),
                        auto_settle=True)
    await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=TODAY)
    txns = await _txns(db_session, seed["org_id"])
    assert len(txns) == 1
    assert txns[0].status == TransactionStatus.SETTLED
    assert txns[0].settled_date == date(2026, 6, 10)
    acct = await db_session.get(Account, seed["account_id"])
    assert acct.balance == Decimal("1000")


async def test_auto_settle_future_is_pending_no_balance(db_session):
    seed = await _seed(db_session, cycle_day=1)
    await _add_template(db_session, seed, type_="income", cat=seed["inc_cat"],
                        amount="1000", freq="monthly", next_due=date(2026, 6, 25),
                        auto_settle=True)
    await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=TODAY)
    txns = await _txns(db_session, seed["org_id"])
    assert txns[0].status == TransactionStatus.PENDING
    assert txns[0].settled_date is None
    acct = await db_session.get(Account, seed["account_id"])
    assert acct.balance == Decimal("0")


async def test_settle_on_due_sweep_promotes_auto_settle_pending(db_session):
    seed = await _seed(db_session, cycle_day=1)
    r = await _add_template(db_session, seed, type_="income", cat=seed["inc_cat"],
                            amount="1000", freq="monthly", next_due=date(2026, 7, 25),
                            auto_settle=True)
    db_session.add(Transaction(
        org_id=seed["org_id"], account_id=seed["account_id"], category_id=seed["inc_cat"],
        description="t", amount=Decimal("1000"), type="income",
        status=TransactionStatus.PENDING, date=date(2026, 6, 10), recurring_id=r.id,
    ))
    await db_session.commit()
    await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=TODAY)
    txns = await _txns(db_session, seed["org_id"])
    swept = [t for t in txns if t.date == date(2026, 6, 10)]
    assert len(swept) == 1
    assert swept[0].status == TransactionStatus.SETTLED
    assert swept[0].settled_date == date(2026, 6, 10)
    acct = await db_session.get(Account, seed["account_id"])
    assert acct.balance == Decimal("1000")


async def test_sweep_leaves_non_auto_settle_pending_alone(db_session):
    seed = await _seed(db_session, cycle_day=1)
    r = await _add_template(db_session, seed, type_="expense", cat=seed["exp_cat"],
                            amount="500", freq="monthly", next_due=date(2026, 7, 5),
                            auto_settle=False)
    db_session.add(Transaction(
        org_id=seed["org_id"], account_id=seed["account_id"], category_id=seed["exp_cat"],
        description="t", amount=Decimal("500"), type="expense",
        status=TransactionStatus.PENDING, date=date(2026, 6, 5), recurring_id=r.id,
    ))
    await db_session.commit()
    await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=TODAY)
    txns = await _txns(db_session, seed["org_id"])
    kept = [t for t in txns if t.date == date(2026, 6, 5)]
    assert kept[0].status == TransactionStatus.PENDING


async def test_overdue_catchup_across_period_boundary(db_session):
    seed = await _seed(db_session, cycle_day=1)
    await _add_template(db_session, seed, type_="expense", cat=seed["exp_cat"],
                        amount="500", freq="monthly", next_due=date(2026, 4, 5),
                        auto_settle=False)
    await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=TODAY)
    txns = await _txns(db_session, seed["org_id"])
    assert [t.date for t in txns] == [date(2026, 4, 5), date(2026, 5, 5), date(2026, 6, 5)]
    assert all(t.status == TransactionStatus.PENDING for t in txns)


async def test_idempotent_second_run_creates_nothing(db_session):
    seed = await _seed(db_session, cycle_day=1)
    await _add_template(db_session, seed, type_="expense", cat=seed["exp_cat"],
                        amount="500", freq="monthly", next_due=date(2026, 6, 25),
                        auto_settle=False)
    await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=TODAY)
    second = await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=TODAY)
    txns = await _txns(db_session, seed["org_id"])
    assert len(txns) == 1
    assert second["generated"] == 0


async def test_weekly_fills_period_and_terminates(db_session):
    seed = await _seed(db_session, cycle_day=1)
    await _add_template(db_session, seed, type_="expense", cat=seed["exp_cat"],
                        amount="10", freq="weekly", next_due=date(2026, 6, 1),
                        auto_settle=False)
    await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=TODAY)
    txns = await _txns(db_session, seed["org_id"])
    assert [t.date for t in txns] == [
        date(2026, 6, 1), date(2026, 6, 8), date(2026, 6, 15),
        date(2026, 6, 22), date(2026, 6, 29),
    ]


async def test_boundary_due_on_period_end_included_next_day_excluded(db_session):
    seed = await _seed(db_session, cycle_day=1)
    await _add_template(db_session, seed, type_="expense", cat=seed["exp_cat"],
                        amount="1", freq="monthly", next_due=date(2026, 6, 30),
                        auto_settle=False)
    await _add_template(db_session, seed, type_="expense", cat=seed["exp_cat"],
                        amount="1", freq="monthly", next_due=date(2026, 7, 1),
                        auto_settle=False)
    await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=TODAY)
    txns = await _txns(db_session, seed["org_id"])
    assert [t.date for t in txns] == [date(2026, 6, 30)]


async def test_dedup_guard_blocks_duplicate_for_same_recurring_and_date(db_session):
    seed = await _seed(db_session, cycle_day=1)
    r = await _add_template(db_session, seed, type_="expense", cat=seed["exp_cat"],
                            amount="500", freq="monthly", next_due=date(2026, 6, 25),
                            auto_settle=False)
    await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=TODAY)
    r.next_due_date = date(2026, 6, 25)
    await db_session.commit()
    await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=TODAY)
    txns = await _txns(db_session, seed["org_id"])
    assert len([t for t in txns if t.date == date(2026, 6, 25)]) == 1
