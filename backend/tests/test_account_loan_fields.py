"""Loan Account Type V1 (Slice 1) — router + type-change integration tests.

Covers create/PUT wiring for the five loan columns and the full type-change
matrix now that a SECOND liability type (loan) coexists with credit_card. The
cascade correctness (esp. CC<->loan keeping/clearing the right columns) is the
load-bearing regression surface for this slice.

Harness mirrors test_account_credit_card_fields.py: FastAPI + async aiosqlite
with FK enforcement ON.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

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
from app.models.cc_cycle_payment import CcCyclePayment
from app.models.user import Role, User
from app.routers.accounts import router as accounts_router
from app.security import hash_password


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


async def _seed(db: AsyncSession) -> dict:
    org = Organization(name="Org", billing_cycle_day=1)
    db.add(org)
    await db.flush()
    admin = User(
        org_id=org.id,
        username="admin",
        email="a@ps.io",
        password_hash=hash_password("pw-1234567"),
        role=Role.ADMIN,
        is_active=True,
        email_verified=True,
    )
    db.add(admin)
    types = {}
    for slug, tname in [
        ("checking", "Checking"),
        ("credit_card", "Credit Card"),
        ("loan", "Loan"),
    ]:
        at = AccountType(org_id=org.id, name=tname, slug=slug, is_system=True)
        db.add(at)
        types[slug] = at
    await db.flush()

    checking = Account(
        org_id=org.id, account_type_id=types["checking"].id, name="Checking",
        balance=Decimal("0.00"), currency="EUR", opening_balance=Decimal("0.00"),
    )
    db.add(checking)
    await db.flush()

    return {
        "org_id": org.id,
        "admin_id": admin.id,
        "type_ids": {s: t.id for s, t in types.items()},
        "checking_id": checking.id,
    }


@pytest_asyncio.fixture
async def world(session_factory) -> dict:
    async with session_factory() as db:
        w = await _seed(db)
        await db.commit()
        return w


def _client(session_factory, user_id: int) -> TestClient:
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_session_factory():
        return session_factory

    async def override_current_user() -> User:
        async with session_factory() as db:
            return (
                await db.execute(select(User).where(User.id == user_id))
            ).scalar_one()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.dependency_overrides[get_current_user] = override_current_user
    app.include_router(accounts_router)
    return TestClient(app)


BASE = "/api/v1/accounts"


async def _row(session_factory, account_id: int) -> Account:
    async with session_factory() as db:
        return (
            await db.execute(select(Account).where(Account.id == account_id))
        ).scalar_one()


_LOAN_BODY = dict(
    principal_amount="10000.00",
    interest_rate_apr="6.00",
    term_months=60,
    origination_date="2026-01-01",
    first_payment_date="2026-01-15",
)


# ── create ────────────────────────────────────────────────────────────────


def test_create_loan_persists_all_five_and_returns_metrics(session_factory, world):
    import asyncio

    client = _client(session_factory, world["admin_id"])
    r = client.post(
        BASE,
        json={
            "name": "Car Loan",
            "account_type_id": world["type_ids"]["loan"],
            "opening_balance": "-10000.00",
            "payment_source_account_id": world["checking_id"],
            **_LOAN_BODY,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["principal_amount"] == "10000.00"
    assert body["term_months"] == 60
    assert body["payment_source_account_id"] == world["checking_id"]
    # nested computed metrics present
    assert body["loan"] is not None
    assert body["loan"]["expected_monthly_payment"] == "193.33"
    assert body["loan"]["status"] == "on_track"

    row = asyncio.get_event_loop().run_until_complete(
        _row(session_factory, body["id"])
    )
    assert row.principal_amount == Decimal("10000.00")
    assert row.first_payment_date is not None


def test_create_loan_missing_field_422(session_factory, world):
    client = _client(session_factory, world["admin_id"])
    partial = dict(_LOAN_BODY)
    del partial["term_months"]
    r = client.post(
        BASE,
        json={
            "name": "Bad Loan",
            "account_type_id": world["type_ids"]["loan"],
            **partial,
        },
    )
    assert r.status_code == 422


def test_create_non_loan_with_loan_field_422(session_factory, world):
    client = _client(session_factory, world["admin_id"])
    r = client.post(
        BASE,
        json={
            "name": "Checking with principal",
            "account_type_id": world["type_ids"]["checking"],
            "principal_amount": "100.00",
        },
    )
    assert r.status_code == 422


def test_create_loan_bad_range_422(session_factory, world):
    client = _client(session_factory, world["admin_id"])
    r = client.post(
        BASE,
        json={
            "name": "Bad term",
            "account_type_id": world["type_ids"]["loan"],
            **{**_LOAN_BODY, "term_months": 0},
        },
    )
    assert r.status_code == 422


# ── PUT wiring ────────────────────────────────────────────────────────────


def test_put_single_loan_field_updates(session_factory, world):
    import asyncio

    client = _client(session_factory, world["admin_id"])
    created = client.post(
        BASE,
        json={
            "name": "Loan",
            "account_type_id": world["type_ids"]["loan"],
            "opening_balance": "-10000.00",
            **_LOAN_BODY,
        },
    ).json()
    r = client.put(f"{BASE}/{created['id']}", json={"principal_amount": "12000.00"})
    assert r.status_code == 200, r.text
    row = asyncio.get_event_loop().run_until_complete(
        _row(session_factory, created["id"])
    )
    assert row.principal_amount == Decimal("12000.00")


# ── type-change matrix ────────────────────────────────────────────────────


def _make_loan(client, world, *, opening="-10000.00", payment_source=None):
    body = {
        "name": "Loan",
        "account_type_id": world["type_ids"]["loan"],
        "opening_balance": opening,
        **_LOAN_BODY,
    }
    if payment_source is not None:
        body["payment_source_account_id"] = payment_source
    return client.post(BASE, json=body).json()


def test_asset_to_loan_sets_cols_keeps_payment_source(session_factory, world):
    import asyncio

    client = _client(session_factory, world["admin_id"])
    # A second checking account to serve as the (distinct) payment source.
    source = client.post(
        BASE,
        json={
            "name": "Source Checking",
            "account_type_id": world["type_ids"]["checking"],
            "opening_balance": "0.00",
        },
    ).json()
    # Convert the seeded checking -> loan, supplying loan fields + a source.
    r = client.put(
        f"{BASE}/{world['checking_id']}",
        json={
            "account_type_id": world["type_ids"]["loan"],
            "payment_source_account_id": source["id"],
            **_LOAN_BODY,
        },
    )
    assert r.status_code == 200, r.text
    row = asyncio.get_event_loop().run_until_complete(
        _row(session_factory, world["checking_id"])
    )
    assert row.principal_amount == Decimal("10000.00")
    assert row.payment_source_account_id == source["id"]


def test_cc_to_loan_keeps_source_clears_cc_and_cycle_payments(session_factory, world):
    import asyncio

    client = _client(session_factory, world["admin_id"])
    # Create a CC with a payment source + credit_limit, then add a cycle payment.
    cc = client.post(
        BASE,
        json={
            "name": "Visa",
            "account_type_id": world["type_ids"]["credit_card"],
            "close_day": 15,
            "credit_limit": "5000.00",
            "payment_source_account_id": world["checking_id"],
        },
    ).json()

    async def _add_cycle_payment():
        async with session_factory() as db:
            db.add(
                CcCyclePayment(
                    account_id=cc["id"],
                    period_anchor_year=2026,
                    period_anchor_month=2,
                    amount=Decimal("100.00"),
                )
            )
            await db.commit()

    asyncio.get_event_loop().run_until_complete(_add_cycle_payment())

    r = client.put(
        f"{BASE}/{cc['id']}",
        json={"account_type_id": world["type_ids"]["loan"], **_LOAN_BODY},
    )
    assert r.status_code == 200, r.text

    row = asyncio.get_event_loop().run_until_complete(_row(session_factory, cc["id"]))
    # payment_source KEPT (loan needs it)
    assert row.payment_source_account_id == world["checking_id"]
    # CC metadata cleared
    assert row.credit_limit is None
    assert row.close_day is None
    # loan cols set
    assert row.principal_amount == Decimal("10000.00")

    async def _cycle_count() -> int:
        async with session_factory() as db:
            rows = (
                await db.execute(
                    select(CcCyclePayment).where(CcCyclePayment.account_id == cc["id"])
                )
            ).scalars().all()
            return len(rows)

    assert asyncio.get_event_loop().run_until_complete(_cycle_count()) == 0


def test_loan_to_cc_clears_loan_cols_keeps_source(session_factory, world):
    import asyncio

    client = _client(session_factory, world["admin_id"])
    loan = _make_loan(client, world, opening="0.00", payment_source=world["checking_id"])

    r = client.put(
        f"{BASE}/{loan['id']}",
        json={"account_type_id": world["type_ids"]["credit_card"], "close_day": 10},
    )
    assert r.status_code == 200, r.text
    row = asyncio.get_event_loop().run_until_complete(_row(session_factory, loan["id"]))
    # loan cols cleared
    assert row.principal_amount is None
    assert row.term_months is None
    assert row.first_payment_date is None
    # payment_source kept (still a liability), close_day set
    assert row.payment_source_account_id == world["checking_id"]
    assert row.close_day == 10


def test_loan_to_asset_clears_loan_cols_and_source(session_factory, world):
    import asyncio

    client = _client(session_factory, world["admin_id"])
    loan = _make_loan(client, world, opening="0.00", payment_source=world["checking_id"])

    r = client.put(
        f"{BASE}/{loan['id']}",
        json={"account_type_id": world["type_ids"]["checking"]},
    )
    assert r.status_code == 200, r.text
    row = asyncio.get_event_loop().run_until_complete(_row(session_factory, loan["id"]))
    assert row.principal_amount is None
    assert row.interest_rate_apr is None
    # leaving all liabilities clears the pointer
    assert row.payment_source_account_id is None


def test_loan_to_loan_is_idempotent(session_factory, world):
    import asyncio

    client = _client(session_factory, world["admin_id"])
    loan = _make_loan(client, world)

    r = client.put(
        f"{BASE}/{loan['id']}",
        json={"account_type_id": world["type_ids"]["loan"]},
    )
    assert r.status_code == 200, r.text
    row = asyncio.get_event_loop().run_until_complete(_row(session_factory, loan["id"]))
    assert row.principal_amount == Decimal("10000.00")
    assert row.term_months == 60
