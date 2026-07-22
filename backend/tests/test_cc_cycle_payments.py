"""Per-cycle credit-card payment amounts — roundtrip test (Credit Card Model
V1, Slice 2, Task 1).

Backend stack mirrors ``test_account_payment_source.py``: FastAPI +
SQLAlchemy 2.0 async over in-memory aiosqlite with FK enforcement ON.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Account, AccountType, Organization
from app.models.account import PaymentStrategy
from app.models.base import Base
from app.models.cc_cycle_payment import CcCyclePayment
from app.models.user import Role, User
from app.routers.accounts import router as accounts_router
from app.routers.cc_cycle_payments import router as cc_cycle_payments_router
from app.security import hash_password
from app.services import cc_cycle_payment_service as svc


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

    member = User(
        org_id=org.id,
        username=f"member-{name}",
        email=f"member-{email}",
        password_hash=hash_password("pw-1234567"),
        role=Role.MEMBER,
        is_active=True,
        email_verified=True,
    )
    db.add(member)

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
        "member_id": member.id,
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
    app.include_router(cc_cycle_payments_router)
    return app


async def _account_row(session_factory, account_id: int) -> Account:
    async with session_factory() as db:
        return (
            await db.execute(select(Account).where(Account.id == account_id))
        ).scalar_one()


def test_cc_cycle_payment_roundtrips(session_factory, worlds):
    """A cc_cycle_payments row persists and reads back, anchored to the
    close month with a NOT NULL amount."""
    import asyncio

    a = worlds["a"]

    async def _write_and_read() -> CcCyclePayment:
        async with session_factory() as db:
            db.add(
                CcCyclePayment(
                    account_id=a["cc_id"],
                    period_anchor_year=2026,
                    period_anchor_month=8,
                    amount=Decimal("125.00"),
                )
            )
            await db.commit()
        async with session_factory() as db:
            return (
                await db.execute(
                    select(CcCyclePayment).where(
                        CcCyclePayment.account_id == a["cc_id"]
                    )
                )
            ).scalar_one()

    row = asyncio.get_event_loop().run_until_complete(_write_and_read())
    assert row.period_anchor_year == 2026
    assert row.period_anchor_month == 8
    assert row.amount == Decimal("125.00")
    assert row.created_at is not None


class _FakeAccount:
    """Minimal stand-in exposing the three resolver columns."""

    def __init__(self, *, close_day=None, payment_day=None, payment_day_relative_month=None):
        self.close_day = close_day
        self.payment_day = payment_day
        self.payment_day_relative_month = payment_day_relative_month


def test_upcoming_cycles_returns_three_distinct_forward_cycles():
    acct = _FakeAccount(close_day=15)
    today = date(2026, 7, 22)  # after the 15th -> current cycle closes Aug 15
    cycles = svc.upcoming_cycles(acct, today=today)
    assert len(cycles) == 3
    anchors = [(c.period_end_inclusive.year, c.period_end_inclusive.month) for c in cycles]
    assert anchors == [(2026, 8), (2026, 9), (2026, 10)]
    for c in cycles:
        assert c.period_end_inclusive < c.payment_date  # close before due


def test_resolve_anchor_cycle_maps_close_month():
    acct = _FakeAccount(close_day=15)
    cycle = svc.resolve_anchor_cycle(acct, year=2026, month=9)
    assert cycle.period_end_inclusive == date(2026, 9, 15)


def test_resolve_anchor_cycle_non_cc_raises():
    with pytest.raises(ValueError):
        svc.resolve_anchor_cycle(_FakeAccount(close_day=None), year=2026, month=9)


def test_validate_rejects_non_cc_422():
    acct = _FakeAccount(close_day=None)
    with pytest.raises(HTTPException) as exc:
        svc.validate_cycle_payment(
            account=acct, account_slug="checking",
            year=2026, month=9, today=date(2026, 7, 22),
            amount=Decimal("50.00"),
        )
    assert exc.value.status_code == 422


@pytest.mark.parametrize("bad_amount", [Decimal("0"), Decimal("-1")])
def test_validate_rejects_non_positive_amount_422(bad_amount):
    acct = _FakeAccount(close_day=15)
    with pytest.raises(HTTPException) as exc:
        svc.validate_cycle_payment(
            account=acct, account_slug="credit_card",
            year=2026, month=9, today=date(2026, 7, 22),
            amount=bad_amount,
        )
    assert exc.value.status_code == 422


def test_validate_rejects_past_anchor_409():
    acct = _FakeAccount(close_day=15)
    with pytest.raises(HTTPException) as exc:
        svc.validate_cycle_payment(
            account=acct, account_slug="credit_card",
            year=2026, month=6, today=date(2026, 7, 22),
            amount=Decimal("50.00"),
        )
    assert exc.value.status_code == 409


def test_validate_accepts_current_and_future_anchor():
    acct = _FakeAccount(close_day=15)
    today = date(2026, 7, 22)  # current close month = Aug 2026
    svc.validate_cycle_payment(
        account=acct, account_slug="credit_card",
        year=2026, month=8, today=today, amount=Decimal("50.00"),
    )
    svc.validate_cycle_payment(
        account=acct, account_slug="credit_card",
        year=2027, month=1, today=today, amount=Decimal("50.00"),
    )


def test_validate_delete_path_skips_amount_check():
    acct = _FakeAccount(close_day=15)
    svc.validate_cycle_payment(
        account=acct, account_slug="credit_card",
        year=2026, month=8, today=date(2026, 7, 22), amount=None,
    )


# ── endpoint: GET upcoming collection ───────────────────────────────────────


def test_get_upcoming_returns_three_cycles_with_dates(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.get(f"/api/v1/accounts/{a['cc_id']}/cycle-payments")
    assert res.status_code == 200, res.text
    rows = res.json()
    assert len(rows) == 3
    for r in rows:
        assert set(r.keys()) == {"year", "month", "close_date", "due_date", "amount"}
        assert r["amount"] is None
        assert r["close_date"] < r["due_date"]


def test_get_upcoming_non_cc_returns_empty(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.get(f"/api/v1/accounts/{a['checking_id']}/cycle-payments")
    assert res.status_code == 200, res.text
    assert res.json() == []


def test_get_upcoming_cross_org_404(session_factory, worlds):
    a, b = worlds["a"], worlds["b"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.get(f"/api/v1/accounts/{b['cc_id']}/cycle-payments")
    assert res.status_code == 404, res.text


def _first_upcoming_anchor(client, account_id) -> tuple[int, int]:
    rows = client.get(f"/api/v1/accounts/{account_id}/cycle-payments").json()
    return rows[0]["year"], rows[0]["month"]


def test_put_then_get_reflects_amount(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        year, month = _first_upcoming_anchor(client, a["cc_id"])
        put = client.put(
            f"/api/v1/accounts/{a['cc_id']}/cycle-payments/{year}/{month}",
            json={"amount": "200.00"},
        )
        assert put.status_code == 200, put.text
        rows = client.get(f"/api/v1/accounts/{a['cc_id']}/cycle-payments").json()
        hit = next(r for r in rows if r["year"] == year and r["month"] == month)
        assert hit["amount"] == "200.00"


def test_put_upsert_updates_existing(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        year, month = _first_upcoming_anchor(client, a["cc_id"])
        client.put(
            f"/api/v1/accounts/{a['cc_id']}/cycle-payments/{year}/{month}",
            json={"amount": "200.00"},
        )
        upd = client.put(
            f"/api/v1/accounts/{a['cc_id']}/cycle-payments/{year}/{month}",
            json={"amount": "250.00"},
        )
        assert upd.status_code == 200, upd.text
        rows = client.get(f"/api/v1/accounts/{a['cc_id']}/cycle-payments").json()
        hit = next(r for r in rows if r["year"] == year and r["month"] == month)
        assert hit["amount"] == "250.00"


def test_put_zero_amount_rejected_422(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        year, month = _first_upcoming_anchor(client, a["cc_id"])
        res = client.put(
            f"/api/v1/accounts/{a['cc_id']}/cycle-payments/{year}/{month}",
            json={"amount": "0"},
        )
    assert res.status_code == 422, res.text


def test_put_past_cycle_rejected_409(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{a['cc_id']}/cycle-payments/2000/1",
            json={"amount": "50.00"},
        )
    assert res.status_code == 409, res.text


def test_put_non_cc_rejected_422(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{a['checking_id']}/cycle-payments/2030/1",
            json={"amount": "50.00"},
        )
    assert res.status_code == 422, res.text


def test_delete_removes_row(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        year, month = _first_upcoming_anchor(client, a["cc_id"])
        client.put(
            f"/api/v1/accounts/{a['cc_id']}/cycle-payments/{year}/{month}",
            json={"amount": "200.00"},
        )
        res = client.delete(
            f"/api/v1/accounts/{a['cc_id']}/cycle-payments/{year}/{month}"
        )
        assert res.status_code == 200, res.text
        rows = client.get(f"/api/v1/accounts/{a['cc_id']}/cycle-payments").json()
        hit = next(r for r in rows if r["year"] == year and r["month"] == month)
        assert hit["amount"] is None


def test_delete_absent_404(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        year, month = _first_upcoming_anchor(client, a["cc_id"])
        res = client.delete(
            f"/api/v1/accounts/{a['cc_id']}/cycle-payments/{year}/{month}"
        )
    assert res.status_code == 404, res.text


def test_put_non_admin_forbidden(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["member_id"])
    with TestClient(app) as client:
        year, month = _first_upcoming_anchor(client, a["cc_id"])
        res = client.put(
            f"/api/v1/accounts/{a['cc_id']}/cycle-payments/{year}/{month}",
            json={"amount": "50.00"},
        )
    assert res.status_code == 403, res.text


def test_get_allowed_for_non_admin_member(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["member_id"])
    with TestClient(app) as client:
        res = client.get(f"/api/v1/accounts/{a['cc_id']}/cycle-payments")
    assert res.status_code == 200, res.text
    assert len(res.json()) == 3
