"""F5 — roster tail: both forecast endpoints stay 200 and keep the calendar end.

TBD-243. ``period_effective_end`` returns ``None`` on the roster tail (an open
period with nothing later on the roster), and so does
``period_spend_window_end``. The calendar fallback ``p_start + 1 month - 1 day``
is therefore not optional: it is the only reason ``period_end`` stays non-null
and the only reason the ``while d <= window_end`` loops can terminate.

Driven through the ROUTER on purpose, so ``response_model=ForecastResponse``
(``period_end: datetime.date``, required) is the gate. A ``None`` window is not
a silent zero — SQLAlchemy refuses to compile ``col <= None`` and
``None.isoformat()`` raises — so the missing ``None`` arm surfaces as a 500,
which this test is positioned to catch.

Design: ``specs/2026-07-30-forecast-period-window-design.md`` §4 F5.
"""
from __future__ import annotations

import datetime
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest_asyncio
from dateutil.relativedelta import relativedelta
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user
from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.billing import BillingPeriod
from app.models.category import CategoryType
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Role, User
from app.routers.forecast import router as forecast_router
from app.security import hash_password

DAY = datetime.timedelta(days=1)


@pytest_asyncio.fixture
async def session_factory():
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
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_roster_tail(factory) -> dict:
    """One org whose ONLY billing period is the open row: the roster tail."""
    today = datetime.date.today()
    p_start = today - datetime.timedelta(days=5)
    calendar_end = p_start + relativedelta(months=1) - DAY

    async with factory() as db:
        org = Organization(name="Tail", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id, username="owner", email="owner@tail.io",
            password_hash=hash_password("pw-1234567"), role=Role.OWNER,
            is_active=True, email_verified=True,
        )
        at = AccountType(
            org_id=org.id, name="Checking", slug="checking", is_system=True
        )
        db.add_all([user, at])
        await db.flush()
        acct = Account(
            org_id=org.id, name="Main", account_type_id=at.id,
            balance=Decimal("1000.00"), currency="EUR", is_default=True,
        )
        cat = Category(
            org_id=org.id, name="Food", slug="food", type=CategoryType.EXPENSE
        )
        db.add_all([acct, cat])
        await db.flush()
        # Roster tail: an open row and nothing after it.
        db.add(BillingPeriod(org_id=org.id, start_date=p_start))
        db.add_all([
            Transaction(
                org_id=org.id, account_id=acct.id, category_id=cat.id,
                description="in window", amount=Decimal("100.00"),
                type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
                date=p_start + datetime.timedelta(days=2),
                settled_date=p_start + datetime.timedelta(days=2),
            ),
            # Far past the calendar end: the tail window is bounded by the
            # fallback, so this must NOT be counted.
            Transaction(
                org_id=org.id, account_id=acct.id, category_id=cat.id,
                description="past the end", amount=Decimal("500.00"),
                type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
                date=calendar_end + datetime.timedelta(days=10),
                settled_date=calendar_end + datetime.timedelta(days=10),
            ),
            Transaction(
                org_id=org.id, account_id=acct.id, category_id=cat.id,
                description="pending in window", amount=Decimal("25.00"),
                type=TransactionType.EXPENSE, status=TransactionStatus.PENDING,
                date=p_start + datetime.timedelta(days=3), settled_date=None,
            ),
        ])
        await db.commit()
        return {
            "user_id": user.id,
            "org_id": org.id,
            "account_id": acct.id,
            "p_start": p_start,
            "calendar_end": calendar_end,
        }


def _make_app(factory, user_id: int) -> FastAPI:
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    async def override_current_user() -> User:
        async with factory() as session:
            return await session.get(User, user_id)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.include_router(forecast_router)
    return app


async def test_f5_roster_tail_forecast_keeps_the_calendar_end(session_factory):
    """FENCE. Kills a ``window_end`` bound straight to
    ``period_spend_window_end`` with no ``None`` arm."""
    seed = await _seed_roster_tail(session_factory)
    app = _make_app(session_factory, seed["user_id"])

    with TestClient(app) as client:
        resp = client.get("/api/v1/forecast")

    assert resp.status_code == 200
    body = resp.json()
    assert body["period_start"] == seed["p_start"].isoformat()
    assert body["period_end"] == seed["calendar_end"].isoformat()
    # Unchanged from before TBD-243: the tail's window is the calendar end.
    assert Decimal(body["executed_expense"]) == Decimal("100")
    assert Decimal(body["pending_expense"]) == Decimal("25")


async def test_f5_roster_tail_account_balances_keeps_the_calendar_end(session_factory):
    """FENCE. The same ``None`` arm on the account-balance surface."""
    seed = await _seed_roster_tail(session_factory)
    app = _make_app(session_factory, seed["user_id"])

    with TestClient(app) as client:
        resp = client.get("/api/v1/forecast/account-balances")

    assert resp.status_code == 200
    body = resp.json()
    assert body["period_start"] == seed["p_start"].isoformat()
    assert body["period_end"] == seed["calendar_end"].isoformat()
    row = {a["account_id"]: a for a in body["accounts"]}[seed["account_id"]]
    assert Decimal(row["pending_delta"]) == Decimal("-25.00")
    assert Decimal(row["expected_month_end_balance"]) == Decimal("975.00")
