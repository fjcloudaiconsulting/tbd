"""Credit Card Model V1 — ORM columns + native enum (Slice 1, Task 1).

Covers the enum-roundtrip behavior for the four CC-only columns added in
migration 073: ``credit_limit``, ``apr``, ``fixed_payment_amount``, and the
native-enum ``payment_strategy`` column.

Backend stack mirrors ``test_account_payment_source.py``: FastAPI +
SQLAlchemy 2.0 async over in-memory aiosqlite with FK enforcement ON.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Account, AccountType, Organization
from app.models.base import Base
from app.models.user import Role, User
from app.routers.accounts import router as accounts_router
from app.security import hash_password

from app.models.account import PaymentStrategy


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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


async def _seed_org(db: AsyncSession, *, name: str, email: str) -> dict:
    """Create an org with an admin and the four account types this suite
    exercises (checking, savings, credit_card, investment), plus a handful
    of accounts. Returns a dict of the interesting ids."""
    org = Organization(name=name, billing_cycle_day=1)
    db.add(org)
    await db.flush()

    admin = User(
        org_id=org.id,
        username=f"admin-{name}",
        email=email,
        password_hash=hash_password("pw-1234567"),
        role=Role.ADMIN,
        is_active=True,
        email_verified=True,
    )
    db.add(admin)

    types = {}
    for slug, tname in [
        ("checking", "Checking"),
        ("savings", "Savings"),
        ("credit_card", "Credit Card"),
        ("investment", "Investment"),
    ]:
        at = AccountType(org_id=org.id, name=tname, slug=slug, is_system=True)
        db.add(at)
        types[slug] = at
    await db.flush()

    def _acct(slug: str, aname: str, *, is_active: bool = True, close_day=None):
        a = Account(
            org_id=org.id,
            account_type_id=types[slug].id,
            name=aname,
            balance=Decimal("0.00"),
            currency="EUR",
            is_active=is_active,
            close_day=close_day,
            opening_balance=Decimal("0.00"),
        )
        db.add(a)
        return a

    checking = _acct("checking", f"{name} Checking")
    savings = _acct("savings", f"{name} Savings")
    cash_at = AccountType(org_id=org.id, name="Cash", slug="cash", is_system=True)
    db.add(cash_at)
    await db.flush()
    cash = Account(
        org_id=org.id,
        account_type_id=cash_at.id,
        name=f"{name} Cash",
        balance=Decimal("0.00"),
        currency="EUR",
        is_active=True,
        opening_balance=Decimal("0.00"),
    )
    db.add(cash)
    investment = _acct("investment", f"{name} Brokerage")
    inactive_checking = _acct(
        "checking", f"{name} Old Checking", is_active=False
    )
    cc = _acct("credit_card", f"{name} Visa", close_day=15)
    await db.flush()

    return {
        "org_id": org.id,
        "admin_id": admin.id,
        "type_ids": {slug: at.id for slug, at in types.items()} | {"cash": cash_at.id},
        "checking_id": checking.id,
        "savings_id": savings.id,
        "cash_id": cash.id,
        "investment_id": investment.id,
        "inactive_checking_id": inactive_checking.id,
        "cc_id": cc.id,
    }


@pytest_asyncio.fixture
async def worlds(session_factory) -> dict:
    """Two independent orgs (A and B) so cross-org isolation is testable."""
    async with session_factory() as db:
        a = await _seed_org(db, name="OrgA", email="a@ps.io")
        b = await _seed_org(db, name="OrgB", email="b@ps.io")
        await db.commit()
        return {"a": a, "b": b}


def _make_app(session_factory, current_user_id: int) -> FastAPI:
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_session_factory():
        return session_factory

    async def override_current_user() -> User:
        async with session_factory() as db:
            return (
                await db.execute(select(User).where(User.id == current_user_id))
            ).scalar_one()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.dependency_overrides[get_current_user] = override_current_user
    app.include_router(accounts_router)
    return app


async def _account_row(session_factory, account_id: int) -> Account:
    async with session_factory() as db:
        return (
            await db.execute(select(Account).where(Account.id == account_id))
        ).scalar_one()


def test_payment_strategy_enum_roundtrips_lowercase(session_factory, worlds):
    """The native-enum column stores and returns the lowercase value."""
    import asyncio

    a = worlds["a"]

    async def _write_and_read() -> object:
        async with session_factory() as db:
            row = (
                await db.execute(select(Account).where(Account.id == a["cc_id"]))
            ).scalar_one()
            row.payment_strategy = PaymentStrategy.FIXED_AMOUNT
            row.fixed_payment_amount = Decimal("75.00")
            row.credit_limit = Decimal("2000.00")
            row.apr = Decimal("19.99")
            await db.commit()
        async with session_factory() as db:
            return (
                await db.execute(select(Account).where(Account.id == a["cc_id"]))
            ).scalar_one()

    reread = asyncio.get_event_loop().run_until_complete(_write_and_read())
    assert reread.payment_strategy == PaymentStrategy.FIXED_AMOUNT
    assert reread.payment_strategy.value == "fixed_amount"
    assert reread.credit_limit == Decimal("2000.00")
    assert reread.apr == Decimal("19.99")
    assert reread.fixed_payment_amount == Decimal("75.00")
