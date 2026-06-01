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

    assert after["forecast_net"] == before["forecast_net"]
