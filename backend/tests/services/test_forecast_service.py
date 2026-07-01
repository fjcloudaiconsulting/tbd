"""forecast_service period-window bucketing uses the effective settled date.

A PENDING transaction dated in May but carrying a June settled-date estimate
must count toward the June forecast window (cash-basis), consistent with the
transactions list and reports.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.billing import BillingPeriod
from app.models.category import CategoryType
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.schemas.forecast import ForecastResponse
from app.services import forecast_service


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


async def _seed_org(db_session) -> tuple[Organization, Account, Category]:
    org = Organization(name="T", billing_cycle_day=1)
    db_session.add(org)
    await db_session.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db_session.add(at)
    await db_session.flush()
    acct = Account(org_id=org.id, name="Main", account_type_id=at.id,
                   balance=Decimal("0"), currency="EUR")
    db_session.add(acct)
    await db_session.flush()
    cat = Category(org_id=org.id, name="Food", slug="food", type=CategoryType.EXPENSE)
    db_session.add(cat)
    await db_session.flush()
    # Explicit June 2026 period so the window is deterministic.
    db_session.add(BillingPeriod(
        org_id=org.id, start_date=date(2026, 6, 1), end_date=date(2026, 6, 30),
    ))
    await db_session.commit()
    return org, acct, cat


@pytest.mark.asyncio
async def test_pending_window_buckets_by_effective_settled_date(db_session):
    """A PENDING expense dated 2026-05-31 with settled_date 2026-06-15 counts
    in the June forecast window, not the May one.
    """
    org, acct, cat = await _seed_org(db_session)
    db_session.add(Transaction(
        org_id=org.id, account_id=acct.id, category_id=cat.id,
        description="GBLT", amount=Decimal("459.68"),
        type=TransactionType.EXPENSE, status=TransactionStatus.PENDING,
        date=date(2026, 5, 31), settled_date=date(2026, 6, 15),
    ))
    await db_session.commit()

    fc = await forecast_service.compute_forecast(
        db_session, org.id, period_start=date(2026, 6, 1)
    )
    # GBLT must land in June's pending bucket via its settled date.
    assert float(fc["pending_expense"]) == 459.68


@pytest.mark.asyncio
async def test_response_model_validates_and_preserves_wire_shape(db_session):
    """ForecastResponse validates the real compute_forecast payload and, once
    serialised as JSON, reproduces the exact same wire shape the service emits
    today — so adding response_model= to the endpoint is behaviour-preserving.
    """
    org, acct, cat = await _seed_org(db_session)
    # One settled expense and one pending expense so both totals and the
    # per-category breakdown are exercised.
    db_session.add(Transaction(
        org_id=org.id, account_id=acct.id, category_id=cat.id,
        description="Groceries", amount=Decimal("120.50"),
        type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
        date=date(2026, 6, 5), settled_date=date(2026, 6, 5),
    ))
    db_session.add(Transaction(
        org_id=org.id, account_id=acct.id, category_id=cat.id,
        description="Pending", amount=Decimal("30.00"),
        type=TransactionType.EXPENSE, status=TransactionStatus.PENDING,
        date=date(2026, 6, 20), settled_date=date(2026, 6, 20),
    ))
    await db_session.commit()

    fc = await forecast_service.compute_forecast(
        db_session, org.id, period_start=date(2026, 6, 1)
    )

    # Model accepts the real payload.
    model = ForecastResponse.model_validate(fc)

    # Serialised JSON is byte-identical to the untyped dict the service
    # returns today (money fields stay strings, dates stay ISO strings).
    dumped = model.model_dump(mode="json")
    assert dumped == fc
