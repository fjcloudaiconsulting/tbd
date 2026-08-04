"""Forecast net is invariant across a Generate within the same period.

Generation advances next_due_date past period_end, so a future instance moves
from forecast's recurring-projection bucket to its pending bucket with the same
amount — totals are conserved. Guards against double-counting regressions.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.category import CategoryType
from app.models.recurring import RecurringTransaction
from app.services import forecast_service, recurring_service


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


async def test_forecast_net_unchanged_across_generate(db_session):
    today = date.today()
    # Anchor the cycle to today so generation's window and forecast's window align.
    org = Organization(name="T", billing_cycle_day=today.day)
    db_session.add(org)
    await db_session.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db_session.add(at)
    await db_session.flush()
    acct = Account(org_id=org.id, name="Main", account_type_id=at.id,
                   balance=Decimal("0"), currency="EUR")
    db_session.add(acct)
    await db_session.flush()
    exp = Category(org_id=org.id, name="Rent", slug="rent", type=CategoryType.EXPENSE)
    db_session.add(exp)
    await db_session.flush()
    db_session.add(RecurringTransaction(
        org_id=org.id, account_id=acct.id, category_id=exp.id,
        description="rent", amount=Decimal("500"), type="expense",
        frequency="monthly", next_due_date=today + timedelta(days=3),
        auto_settle=False, is_active=True,
    ))
    await db_session.commit()

    before = await forecast_service.compute_forecast(db_session, org.id)
    await recurring_service.generate_due_transactions(db_session, org.id)
    after = await forecast_service.compute_forecast(db_session, org.id)

    # Guard against a vacuous pass: the 500 must actually be PROJECTED before
    # (recurring bucket) and MATERIALIZED after (pending bucket) — that bucket
    # move is exactly the double-count invariant under test.
    assert float(before["recurring_expense"]) > 0
    assert float(after["pending_expense"]) > 0
    assert after["forecast_net"] == before["forecast_net"]


async def test_daily_series_and_risk_days_invariant_across_generate(db_session):
    """F4 (TBD-198). The per-account DAILY series and the risk-day set are
    byte-identical before and after `generate_due_transactions`.

    Same conservation claim as the test above, one resolution finer: generation
    moves an occurrence from the recurring-projection bucket to the pending
    bucket ON THE SAME DATE, so not one point of the series may move -- and a
    warning that appears or disappears when the scheduler ticks is worse than
    no warning at all.

    Mutant killed: a daily series built only from MATERIALISED rows (i.e. the
    recurring projection omitted from `account_balance_forecast_service`, which
    is what it looked like before TBD-198). Before generation that series never
    dips, `risk_days` is empty, and it sprouts a warning the instant the
    scheduler runs.

    NON-VACUITY: the template's occurrence is asserted to be NOT yet
    materialised at the first call, and the pre-generation `risk_days` is
    asserted NON-EMPTY. Without the first, an already-generated template makes
    both calls read the same rows and the test proves nothing; without the
    second, `[] == []` passes against the mutant.
    """
    from sqlalchemy import func, select

    from app.models.transaction import Transaction
    from app.services import account_balance_forecast_service

    today = date.today()
    org = Organization(name="T", billing_cycle_day=today.day)
    db_session.add(org)
    await db_session.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db_session.add(at)
    await db_session.flush()
    acct = Account(org_id=org.id, name="Main", account_type_id=at.id,
                   balance=Decimal("0"), currency="EUR")
    db_session.add(acct)
    await db_session.flush()
    exp = Category(org_id=org.id, name="Rent", slug="rent", type=CategoryType.EXPENSE)
    db_session.add(exp)
    await db_session.flush()
    db_session.add(RecurringTransaction(
        org_id=org.id, account_id=acct.id, category_id=exp.id,
        description="rent", amount=Decimal("500"), type="expense",
        frequency="monthly", next_due_date=today + timedelta(days=3),
        auto_settle=False, is_active=True,
    ))
    await db_session.commit()

    async def _rows_from_template() -> int:
        return (await db_session.execute(
            select(func.count()).select_from(Transaction)
            .where(Transaction.recurring_id.is_not(None))
        )).scalar()

    # PRECONDITION: nothing materialised yet. Without this the "before" call
    # already reads a real row and the comparison is trivially true.
    assert await _rows_from_template() == 0

    before = await account_balance_forecast_service.compute_account_balance_forecast(
        db_session, org.id
    )
    await recurring_service.generate_due_transactions(db_session, org.id)
    assert await _rows_from_template() == 1
    after = await account_balance_forecast_service.compute_account_balance_forecast(
        db_session, org.id
    )

    b_row = next(a for a in before["accounts"] if a["account_id"] == acct.id)
    a_row = next(a for a in after["accounts"] if a["account_id"] == acct.id)

    # Guard against a vacuous pass: the 500 must be PROJECTED before and
    # MATERIALISED after, and the dip must actually be warned about.
    assert b_row["pending_delta"] == "0.00"
    assert a_row["pending_delta"] == "-500.00"
    assert len(b_row["risk_days"]) == 1

    assert a_row["daily_balances"] == b_row["daily_balances"]
    assert a_row["risk_days"] == b_row["risk_days"]
    assert a_row["expected_month_end_balance"] == b_row["expected_month_end_balance"]
