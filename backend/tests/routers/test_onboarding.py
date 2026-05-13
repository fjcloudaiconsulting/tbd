"""Router tests for the L3.3 onboarding endpoints.

Covers:
- ``POST /onboarding/complete`` sets ``users.onboarded_at``.
- Re-calling ``/complete`` updates the timestamp (idempotent, never 409).
- ``POST /onboarding/seed-demo`` seeds and returns counts.
- ``/seed-demo`` returns 409 when the org already has data.
- ``/seed-demo`` writes audit rows on success and refusal.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import datetime

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.rate_limit import limiter
from app.models import Base
from app.models.account import Account, AccountType
from app.models.audit_event import AuditEvent
from app.models.category import Category, CategoryType
from app.models.transaction import (
    Transaction,
    TransactionStatus,
    TransactionType,
)
from app.models.user import Organization, Role, User
from app.routers.onboarding import router as onboarding_router
from app.security import hash_password


@pytest.fixture(autouse=True)
def reset_limiter():
    """Per-IP slowapi counter is a module-level singleton; reset
    between cases so the 3/hour cap on /seed-demo does not bleed."""
    limiter.reset()
    yield
    limiter.reset()


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


async def _seed_user(factory) -> dict:
    async with factory() as db:
        org = Organization(name="Onboard Org", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id, username="newbie",
            email="newbie@example.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER, is_active=True, email_verified=True,
        )
        db.add(user)
        await db.flush()
        at = AccountType(
            org_id=org.id, name="Checking", slug="checking", is_system=True
        )
        db.add(at)
        for slug, name_ in [
            ("paycheck", "Paycheck"),
            ("groceries", "Groceries"),
            ("rent_mortgage", "Rent"),
            ("coffee_shops", "Coffee"),
        ]:
            db.add(
                Category(
                    org_id=org.id, name=name_, slug=slug,
                    is_system=True, type=CategoryType.BOTH,
                )
            )
        await db.commit()
        return {"user_id": user.id, "org_id": org.id, "at_id": at.id}


def make_app(factory, user_id: int) -> FastAPI:
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    async def override_current_user() -> User:
        async with factory() as db:
            return (
                await db.execute(select(User).where(User.id == user_id))
            ).scalar_one()

    def override_session_factory():
        return factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.include_router(onboarding_router)
    return app


@pytest.mark.asyncio
async def test_complete_sets_timestamp(session_factory):
    seeds = await _seed_user(session_factory)
    app = make_app(session_factory, seeds["user_id"])
    with TestClient(app) as client:
        res = client.post("/api/v1/users/me/onboarding/complete")
    assert res.status_code == 200
    body = res.json()
    assert body["onboarded_at"]
    async with session_factory() as db:
        u = (
            await db.execute(select(User).where(User.id == seeds["user_id"]))
        ).scalar_one()
    assert u.onboarded_at is not None


@pytest.mark.asyncio
async def test_complete_is_idempotent(session_factory):
    seeds = await _seed_user(session_factory)
    app = make_app(session_factory, seeds["user_id"])
    with TestClient(app) as client:
        first = client.post("/api/v1/users/me/onboarding/complete")
        second = client.post("/api/v1/users/me/onboarding/complete")
    assert first.status_code == 200
    assert second.status_code == 200


@pytest.mark.asyncio
async def test_seed_demo_happy_path(session_factory):
    seeds = await _seed_user(session_factory)
    app = make_app(session_factory, seeds["user_id"])
    with TestClient(app) as client:
        res = client.post("/api/v1/users/me/onboarding/seed-demo")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["accounts_created"] == 2
    assert body["transactions_created"] > 0
    async with session_factory() as db:
        rows = (await db.execute(select(AuditEvent))).scalars().all()
        types = [r.event_type for r in rows]
        assert "onboarding.seed_demo.applied" in types


@pytest.mark.asyncio
async def test_seed_demo_409_when_org_has_data(session_factory):
    seeds = await _seed_user(session_factory)
    # Drop a real transaction into the org.
    async with session_factory() as db:
        at = (
            await db.execute(
                select(AccountType).where(AccountType.org_id == seeds["org_id"])
            )
        ).scalar_one()
        acct = Account(
            org_id=seeds["org_id"], name="Real", account_type_id=at.id,
            balance=Decimal("0.00"), currency="EUR", is_active=True,
        )
        db.add(acct)
        cat = (
            await db.execute(
                select(Category).where(
                    Category.org_id == seeds["org_id"],
                    Category.slug == "groceries",
                )
            )
        ).scalar_one()
        await db.flush()
        tx = Transaction(
            org_id=seeds["org_id"], account_id=acct.id, category_id=cat.id,
            description="Real", amount=Decimal("1.00"),
            type=TransactionType.EXPENSE,
            status=TransactionStatus.SETTLED,
            date=datetime.date(2026, 5, 1),
            settled_date=datetime.date(2026, 5, 1),
        )
        db.add(tx)
        await db.commit()

    app = make_app(session_factory, seeds["user_id"])
    with TestClient(app) as client:
        res = client.post("/api/v1/users/me/onboarding/seed-demo")
    assert res.status_code == 409
    assert res.json()["detail"] == "org_has_data"
    async with session_factory() as db:
        rows = (await db.execute(select(AuditEvent))).scalars().all()
        types = [r.event_type for r in rows]
        assert "onboarding.seed_demo.refused" in types


@pytest.mark.asyncio
async def test_seed_demo_409_on_second_call(session_factory):
    seeds = await _seed_user(session_factory)
    app = make_app(session_factory, seeds["user_id"])
    with TestClient(app) as client:
        first = client.post("/api/v1/users/me/onboarding/seed-demo")
        second = client.post("/api/v1/users/me/onboarding/seed-demo")
    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"] == "org_has_data"
