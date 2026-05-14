"""Router tests for the L3.3 onboarding endpoints.

Covers:
- ``POST /onboarding/complete`` sets ``users.onboarded_at``.
- Re-calling ``/complete`` updates the timestamp (idempotent, never 409).
- ``POST /onboarding/seed-demo`` seeds and returns counts.
- ``/seed-demo`` returns 409 when the org already has data.
- ``/seed-demo`` writes audit rows on success and refusal.
- ``POST /onboarding/restart-tour`` records a replay request without
  mutating ``users.onboarded_at`` (preventing the AppShell redirect
  loop), and writes an ``onboarding.tour.restarted`` audit row.
- ``/restart-tour`` is idempotent for both NULL and non-NULL start
  states, leaving the column untouched and auditing every call.
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


@pytest.mark.asyncio
async def test_restart_tour_records_replay_request_leaves_state_intact(
    session_factory,
):
    """Replay-tour must NOT touch onboarded_at.

    AppShell redirects users with ``onboarded_at IS NULL`` to
    ``/onboarding``, so clearing the column on restart would trap the
    user in a wizard redirect loop instead of letting them see the
    dashboard tour overlay. The endpoint stays as a recorded,
    rate-limited audit action that preserves the prior timestamp.
    """
    seeds = await _seed_user(session_factory)
    # First complete onboarding so onboarded_at is non-null.
    prior = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    async with session_factory() as db:
        u = (
            await db.execute(select(User).where(User.id == seeds["user_id"]))
        ).scalar_one()
        u.onboarded_at = prior
        await db.commit()

    app = make_app(session_factory, seeds["user_id"])
    with TestClient(app) as client:
        res = client.post("/api/v1/users/me/onboarding/restart-tour")
    assert res.status_code == 200, res.text
    # Response echoes the existing timestamp, NOT null.
    assert res.json()["onboarded_at"] is not None

    async with session_factory() as db:
        u = (
            await db.execute(select(User).where(User.id == seeds["user_id"]))
        ).scalar_one()
    # State invariant: onboarded_at is unchanged from before the call.
    # This is the data-level proxy for "AppShell will not redirect to
    # /onboarding after the user clicks Replay tour".
    assert u.onboarded_at == prior

    async with session_factory() as db:
        rows = (await db.execute(select(AuditEvent))).scalars().all()
        types = [r.event_type for r in rows]
        assert "onboarding.tour.restarted" in types


@pytest.mark.asyncio
async def test_restart_tour_is_idempotent(session_factory):
    """Restart-tour leaves state untouched whether onboarded_at was
    NULL or already a timestamp, and audits each call.
    """
    seeds = await _seed_user(session_factory)

    # Case 1: starting from NULL (user has not finished the wizard).
    app = make_app(session_factory, seeds["user_id"])
    with TestClient(app) as client:
        first = client.post("/api/v1/users/me/onboarding/restart-tour")
        second = client.post("/api/v1/users/me/onboarding/restart-tour")
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["onboarded_at"] is None
    assert second.json()["onboarded_at"] is None

    async with session_factory() as db:
        u = (
            await db.execute(select(User).where(User.id == seeds["user_id"]))
        ).scalar_one()
    assert u.onboarded_at is None

    # Case 2: starting from a non-NULL timestamp. Both calls preserve it.
    prior = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    async with session_factory() as db:
        u = (
            await db.execute(select(User).where(User.id == seeds["user_id"]))
        ).scalar_one()
        u.onboarded_at = prior
        await db.commit()

    with TestClient(app) as client:
        third = client.post("/api/v1/users/me/onboarding/restart-tour")
        fourth = client.post("/api/v1/users/me/onboarding/restart-tour")
    assert third.status_code == 200
    assert fourth.status_code == 200
    assert third.json()["onboarded_at"] is not None
    assert fourth.json()["onboarded_at"] is not None

    async with session_factory() as db:
        u = (
            await db.execute(select(User).where(User.id == seeds["user_id"]))
        ).scalar_one()
    assert u.onboarded_at == prior

    async with session_factory() as db:
        rows = (await db.execute(select(AuditEvent))).scalars().all()
        restarted = [r for r in rows if r.event_type == "onboarding.tour.restarted"]
    # All four calls produce an audit row.
    assert len(restarted) == 4


@pytest.mark.asyncio
async def test_restart_tour_preserves_value_so_appshell_does_not_loop(
    session_factory,
):
    """Direct invariant: post-restart, current_user.onboarded_at is
    unchanged from the pre-call value. AppShell guards on
    ``onboarded_at === null`` for its /onboarding redirect, so
    preserving a non-NULL value is what prevents the loop.
    """
    seeds = await _seed_user(session_factory)
    prior = datetime.datetime(2026, 5, 1, 9, 30, 0)
    async with session_factory() as db:
        u = (
            await db.execute(select(User).where(User.id == seeds["user_id"]))
        ).scalar_one()
        u.onboarded_at = prior
        await db.commit()

    app = make_app(session_factory, seeds["user_id"])
    with TestClient(app) as client:
        res = client.post("/api/v1/users/me/onboarding/restart-tour")
    assert res.status_code == 200

    async with session_factory() as db:
        u_after = (
            await db.execute(select(User).where(User.id == seeds["user_id"]))
        ).scalar_one()
    assert u_after.onboarded_at == prior
    assert u_after.onboarded_at is not None
