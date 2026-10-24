"""BE-14: account update cannot be used by a MEMBER to do what the admin-only
money endpoints guard.

- Changing ``opening_balance`` on an account with settled transactions moves
  the live balance with no ledger row (the job of admin-only
  ``/adjust-balance``), so it needs OWNER/ADMIN. A MEMBER may still correct
  the opening balance of an account with no settled activity, and the
  frontend's habit of re-sending the unchanged value stays harmless.
- Moving a credit card off ``credit_card`` deletes its per-cycle payment rows,
  whose own endpoints are owner/admin only, so that needs OWNER/ADMIN too.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.category import CategoryType
from app.models.cc_cycle_payment import CcCyclePayment
from app.models.user import Role, User
from app.routers.accounts import router as accounts_router
from app.schemas.transaction import TransactionCreate
from app.services import transaction_service


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


@pytest_asyncio.fixture
async def seed(session_factory) -> dict:
    async with session_factory() as db:
        org = Organization(name="Household", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        users = {}
        for role in (Role.ADMIN, Role.MEMBER):
            u = User(
                org_id=org.id, username=role.value, email=f"{role.value}@h.io",
                password_hash="x", role=role, is_active=True, email_verified=True,
            )
            db.add(u)
            users[role] = u
        checking = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
        cc = AccountType(org_id=org.id, name="Credit Card", slug="credit_card", is_system=True)
        cat = Category(
            org_id=org.id, name="General", slug="general",
            type=CategoryType.BOTH, is_system=True,
        )
        db.add_all([checking, cc, cat])
        await db.flush()

        def _acct(name, type_, **kw):
            a = Account(
                org_id=org.id, account_type_id=type_.id, name=name,
                balance=Decimal("100.00"), opening_balance=Decimal("100.00"),
                currency="EUR", is_active=True, **kw,
            )
            db.add(a)
            return a

        active = _acct("Joint", checking)
        fresh = _acct("New", checking)
        card = _acct("Visa", cc, close_day=15)
        bare_card = _acct("Spare Visa", cc, close_day=15)
        await db.flush()
        db.add(CcCyclePayment(
            account_id=card.id, period_anchor_year=2026, period_anchor_month=8,
            amount=Decimal("125.00"),
        ))
        await transaction_service.create_transaction(db, org.id, TransactionCreate(
            account_id=active.id, category_id=cat.id, description="groceries",
            amount=Decimal("40.00"), type="expense", status="settled",
            date=date(2026, 6, 1),
        ))
        await db.commit()
        return {
            "user_ids": {r: u.id for r, u in users.items()},
            "checking_type_id": checking.id,
            "active_id": active.id,
            "fresh_id": fresh.id,
            "card_id": card.id,
            "bare_card_id": bare_card.id,
        }


def _client(session_factory, user_id: int) -> TestClient:
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user() -> User:
        async with session_factory() as db:
            return (await db.execute(select(User).where(User.id == user_id))).scalar_one()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_current_user] = override_current_user
    app.include_router(accounts_router)
    return TestClient(app)


async def _account(session_factory, account_id: int) -> Account:
    async with session_factory() as db:
        return await db.get(Account, account_id)


async def _cycle_payments(session_factory, account_id: int) -> int:
    async with session_factory() as db:
        return await db.scalar(
            select(func.count()).select_from(CcCyclePayment)
            .where(CcCyclePayment.account_id == account_id)
        )


# ── opening_balance ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_member_cannot_rewrite_opening_balance_under_settled_activity(session_factory, seed):
    with _client(session_factory, seed["user_ids"][Role.MEMBER]) as client:
        res = client.put(f"/api/v1/accounts/{seed['active_id']}", json={
            "name": "Renamed", "opening_balance": "5000.00",
        })
    assert res.status_code == 403, res.text
    acct = await _account(session_factory, seed["active_id"])
    assert acct.opening_balance == Decimal("100.00")
    assert acct.balance == Decimal("60.00")
    assert acct.name == "Joint"  # nothing else from the refused PUT landed


@pytest.mark.asyncio
async def test_member_edit_resending_unchanged_opening_balance_still_works(session_factory, seed):
    """The account form always sends opening_balance; an unchanged value is
    not a change and must not block a MEMBER's rename."""
    with _client(session_factory, seed["user_ids"][Role.MEMBER]) as client:
        res = client.put(f"/api/v1/accounts/{seed['active_id']}", json={
            "name": "Renamed", "opening_balance": "100.00",
            "account_type_id": seed["checking_type_id"],
        })
    assert res.status_code == 200, res.text
    assert res.json()["name"] == "Renamed"


@pytest.mark.asyncio
async def test_member_can_fix_opening_balance_before_any_settled_activity(session_factory, seed):
    with _client(session_factory, seed["user_ids"][Role.MEMBER]) as client:
        res = client.put(f"/api/v1/accounts/{seed['fresh_id']}", json={"opening_balance": "250.00"})
    assert res.status_code == 200, res.text
    assert Decimal(res.json()["balance"]) == Decimal("250.00")


@pytest.mark.asyncio
async def test_admin_can_still_change_opening_balance_under_settled_activity(session_factory, seed):
    with _client(session_factory, seed["user_ids"][Role.ADMIN]) as client:
        res = client.put(f"/api/v1/accounts/{seed['active_id']}", json={"opening_balance": "150.00"})
    assert res.status_code == 200, res.text
    assert Decimal(res.json()["balance"]) == Decimal("110.00")


@pytest.mark.asyncio
async def test_member_refused_on_the_atomic_path_too(session_factory, seed):
    """Payload that also touches an account_type_id takes the locked path."""
    with _client(session_factory, seed["user_ids"][Role.MEMBER]) as client:
        res = client.put(f"/api/v1/accounts/{seed['active_id']}", json={
            "opening_balance": "5000.00", "account_type_id": seed["checking_type_id"],
        })
    assert res.status_code == 403, res.text
    assert (await _account(session_factory, seed["active_id"])).balance == Decimal("60.00")


# ── leaving credit_card ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_member_cannot_delete_cycle_payments_by_changing_type(session_factory, seed):
    with _client(session_factory, seed["user_ids"][Role.MEMBER]) as client:
        res = client.put(f"/api/v1/accounts/{seed['card_id']}", json={
            "account_type_id": seed["checking_type_id"],
        })
    assert res.status_code == 403, res.text
    assert await _cycle_payments(session_factory, seed["card_id"]) == 1
    acct = await _account(session_factory, seed["card_id"])
    assert acct.account_type_id != seed["checking_type_id"]
    assert acct.close_day == 15


@pytest.mark.asyncio
async def test_member_can_change_type_of_card_without_cycle_payments(session_factory, seed):
    with _client(session_factory, seed["user_ids"][Role.MEMBER]) as client:
        res = client.put(f"/api/v1/accounts/{seed['bare_card_id']}", json={
            "account_type_id": seed["checking_type_id"],
        })
    assert res.status_code == 200, res.text
    assert res.json()["account_type_slug"] == "checking"


@pytest.mark.asyncio
async def test_admin_can_change_type_and_delete_cycle_payments(session_factory, seed):
    with _client(session_factory, seed["user_ids"][Role.ADMIN]) as client:
        res = client.put(f"/api/v1/accounts/{seed['card_id']}", json={
            "account_type_id": seed["checking_type_id"],
        })
    assert res.status_code == 200, res.text
    assert await _cycle_payments(session_factory, seed["card_id"]) == 0
