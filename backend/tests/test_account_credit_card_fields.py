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
from app.services.credit_card_service import validate_credit_card_fields
from fastapi import HTTPException


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


# ── credit_card_service.validate_credit_card_fields (pure unit) ────────────


def _expect_422(**kwargs) -> HTTPException:
    with pytest.raises(HTTPException) as exc:
        validate_credit_card_fields(**kwargs)
    assert exc.value.status_code == 422
    return exc.value


def test_non_cc_forbids_all_four_cc_fields():
    for field, value in [
        ("credit_limit", Decimal("100.00")),
        ("apr", Decimal("10.00")),
        ("payment_strategy", PaymentStrategy.FULL_BALANCE),
        ("fixed_payment_amount", Decimal("10.00")),
    ]:
        base = dict(
            target_slug="checking",
            credit_limit=None,
            apr=None,
            payment_strategy=None,
            fixed_payment_amount=None,
        )
        base[field] = value
        _expect_422(**base)


def test_cc_allows_all_null():
    # Bare CC account with nothing set is valid (limit optional, strategy NULL).
    validate_credit_card_fields(
        target_slug="credit_card",
        credit_limit=None,
        apr=None,
        payment_strategy=None,
        fixed_payment_amount=None,
    )


def test_cc_credit_limit_must_be_positive_when_set():
    _expect_422(
        target_slug="credit_card",
        credit_limit=Decimal("0"),
        apr=None,
        payment_strategy=None,
        fixed_payment_amount=None,
    )
    validate_credit_card_fields(
        target_slug="credit_card",
        credit_limit=Decimal("2500.00"),
        apr=None,
        payment_strategy=None,
        fixed_payment_amount=None,
    )


@pytest.mark.parametrize("bad_apr", [Decimal("-1"), Decimal("100.01")])
def test_cc_apr_out_of_range_rejected(bad_apr):
    _expect_422(
        target_slug="credit_card",
        credit_limit=None,
        apr=bad_apr,
        payment_strategy=None,
        fixed_payment_amount=None,
    )


@pytest.mark.parametrize("ok_apr", [Decimal("0"), Decimal("19.99"), Decimal("100")])
def test_cc_apr_in_range_ok(ok_apr):
    validate_credit_card_fields(
        target_slug="credit_card",
        credit_limit=None,
        apr=ok_apr,
        payment_strategy=None,
        fixed_payment_amount=None,
    )


def test_fixed_amount_requires_positive_fixed_payment():
    _expect_422(
        target_slug="credit_card",
        credit_limit=None,
        apr=None,
        payment_strategy=PaymentStrategy.FIXED_AMOUNT,
        fixed_payment_amount=None,
    )
    _expect_422(
        target_slug="credit_card",
        credit_limit=None,
        apr=None,
        payment_strategy=PaymentStrategy.FIXED_AMOUNT,
        fixed_payment_amount=Decimal("0"),
    )
    validate_credit_card_fields(
        target_slug="credit_card",
        credit_limit=None,
        apr=None,
        payment_strategy=PaymentStrategy.FIXED_AMOUNT,
        fixed_payment_amount=Decimal("50.00"),
    )


@pytest.mark.parametrize(
    "strategy",
    [
        PaymentStrategy.FULL_BALANCE,
        PaymentStrategy.MINIMUM_ONLY,
        PaymentStrategy.CUSTOM_PER_PERIOD,
        None,
    ],
)
def test_fixed_payment_forbidden_for_non_fixed_strategy(strategy):
    _expect_422(
        target_slug="credit_card",
        credit_limit=None,
        apr=None,
        payment_strategy=strategy,
        fixed_payment_amount=Decimal("50.00"),
    )


# ── schema / read compatibility ────────────────────────────────────────────


def test_read_exposes_all_four_cc_fields(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        body = client.get(f"/api/v1/accounts/{a['cc_id']}").json()
    for key in ("credit_limit", "apr", "payment_strategy", "fixed_payment_amount"):
        assert key in body
        assert body[key] is None


# ── create path ────────────────────────────────────────────────────────────


def test_create_cc_with_fixed_amount_persists_fields(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/accounts",
            json={
                "name": "Rewards Visa",
                "account_type_id": a["type_ids"]["credit_card"],
                "currency": "EUR",
                "close_day": 12,
                "credit_limit": "3000.00",
                "apr": "19.99",
                "payment_strategy": "fixed_amount",
                "fixed_payment_amount": "100.00",
            },
        )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["credit_limit"] == "3000.00"
    assert body["apr"] == "19.99"
    assert body["payment_strategy"] == "fixed_amount"
    assert body["fixed_payment_amount"] == "100.00"


def test_create_non_cc_with_credit_limit_rejected(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/accounts",
            json={
                "name": "Bad Checking",
                "account_type_id": a["type_ids"]["checking"],
                "currency": "EUR",
                "credit_limit": "1000.00",
            },
        )
    assert res.status_code == 422, res.text


def test_create_cc_fixed_amount_without_fixed_payment_rejected(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/accounts",
            json={
                "name": "Half Visa",
                "account_type_id": a["type_ids"]["credit_card"],
                "currency": "EUR",
                "close_day": 12,
                "payment_strategy": "fixed_amount",
            },
        )
    assert res.status_code == 422, res.text


# ── PUT path ────────────────────────────────────────────────────────────────


def test_put_sets_cc_fields_on_existing_cc(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{a['cc_id']}",
            json={
                "credit_limit": "5000.00",
                "apr": "22.50",
                "payment_strategy": "minimum_only",
            },
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["credit_limit"] == "5000.00"
    assert body["apr"] == "22.50"
    assert body["payment_strategy"] == "minimum_only"
    assert body["fixed_payment_amount"] is None


def test_put_fixed_amount_requires_fixed_payment(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{a['cc_id']}",
            json={"payment_strategy": "fixed_amount"},
        )
    assert res.status_code == 422, res.text


def test_put_credit_limit_on_non_cc_rejected(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{a['checking_id']}",
            json={"credit_limit": "1000.00"},
        )
    assert res.status_code == 422, res.text


def test_put_switch_to_fixed_amount_with_payment_ok(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{a['cc_id']}",
            json={
                "payment_strategy": "fixed_amount",
                "fixed_payment_amount": "125.00",
            },
        )
    assert res.status_code == 200, res.text
    assert res.json()["fixed_payment_amount"] == "125.00"


# ── leave-CC cascade ────────────────────────────────────────────────────────


def test_leaving_cc_clears_all_four_cc_fields(session_factory, worlds):
    """Converting a CC to a non-CC type must null every CC-only column, so
    an asset row never silently retains a credit_limit/strategy no UI can
    surface (same bug class as the payment_source leave-CC cascade)."""
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        set_res = client.put(
            f"/api/v1/accounts/{a['cc_id']}",
            json={
                "credit_limit": "4000.00",
                "apr": "18.00",
                "payment_strategy": "fixed_amount",
                "fixed_payment_amount": "90.00",
            },
        )
        assert set_res.status_code == 200, set_res.text

        conv_res = client.put(
            f"/api/v1/accounts/{a['cc_id']}",
            json={"account_type_id": a["type_ids"]["checking"]},
        )
        assert conv_res.status_code == 200, conv_res.text
        body = conv_res.json()
        assert body["credit_limit"] is None
        assert body["apr"] is None
        assert body["payment_strategy"] is None
        assert body["fixed_payment_amount"] is None

    import asyncio

    row = asyncio.get_event_loop().run_until_complete(
        _account_row(session_factory, a["cc_id"])
    )
    assert row.credit_limit is None
    assert row.apr is None
    assert row.payment_strategy is None
    assert row.fixed_payment_amount is None
