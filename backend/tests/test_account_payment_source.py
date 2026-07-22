"""Payment Source Foundation — payment_source_account_id plumbing.

Covers the validation, org-isolation, deletion/deactivation, and read
compatibility surface locked in
``specs/payment-source-account-foundation.md`` § "Tests":

1. Same-org validation — a cross-org source is rejected (422).
2. Org isolation read — org A's account list never surfaces an org B
   account, and a cross-org source can't be assigned.
3. Type allowlist — credit_card / investment sources are rejected (422).
4. Self-pay prevention — an account cannot be its own source (422).
5. Deletion / deactivation — deleting the source SET NULLs the target's
   pointer (FK ON DELETE SET NULL); an inactive source is rejected at
   write time (422).
6. Read compatibility — existing account reads return the same shape with
   the new nullable field present.

Backend stack mirrors ``test_account_opening_balance.py``: FastAPI +
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


# ── 1 & Read compat: happy path + response exposure ────────────────────────


def test_create_cc_with_valid_checking_source(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/accounts",
            json={
                "name": "New Visa",
                "account_type_id": a["type_ids"]["credit_card"],
                "currency": "EUR",
                "close_day": 10,
                "payment_source_account_id": a["checking_id"],
            },
        )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["payment_source_account_id"] == a["checking_id"]


def test_update_set_source_then_clear(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        set_res = client.put(
            f"/api/v1/accounts/{a['cc_id']}",
            json={"payment_source_account_id": a["savings_id"]},
        )
        assert set_res.status_code == 200, set_res.text
        assert set_res.json()["payment_source_account_id"] == a["savings_id"]

        clear_res = client.put(
            f"/api/v1/accounts/{a['cc_id']}",
            json={"payment_source_account_id": None},
        )
        assert clear_res.status_code == 200, clear_res.text
        assert clear_res.json()["payment_source_account_id"] is None


def test_cash_source_is_allowed(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{a['cc_id']}",
            json={"payment_source_account_id": a["cash_id"]},
        )
    assert res.status_code == 200, res.text
    assert res.json()["payment_source_account_id"] == a["cash_id"]


def test_read_compat_existing_account_has_null_field(session_factory, worlds):
    """Forecast/schema read compatibility: a plain account with no source
    still returns 200 and carries the new nullable field."""
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        get_res = client.get(f"/api/v1/accounts/{a['checking_id']}")
        assert get_res.status_code == 200, get_res.text
        assert get_res.json()["payment_source_account_id"] is None

        list_res = client.get("/api/v1/accounts")
        assert list_res.status_code == 200, list_res.text
        for row in list_res.json():
            assert "payment_source_account_id" in row


# ── 2: org isolation ───────────────────────────────────────────────────────


def test_cross_org_source_rejected_on_create(session_factory, worlds):
    a, b = worlds["a"], worlds["b"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/accounts",
            json={
                "name": "Sneaky Visa",
                "account_type_id": a["type_ids"]["credit_card"],
                "currency": "EUR",
                "close_day": 10,
                "payment_source_account_id": b["checking_id"],
            },
        )
    assert res.status_code == 422, res.text


def test_cross_org_source_rejected_on_update(session_factory, worlds):
    a, b = worlds["a"], worlds["b"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{a['cc_id']}",
            json={"payment_source_account_id": b["checking_id"]},
        )
    assert res.status_code == 422, res.text


def test_org_a_list_never_shows_org_b_accounts(session_factory, worlds):
    a, b = worlds["a"], worlds["b"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        rows = client.get("/api/v1/accounts").json()
    ids = {r["id"] for r in rows}
    assert b["checking_id"] not in ids
    assert b["cc_id"] not in ids


# ── 3: type allowlist ──────────────────────────────────────────────────────


@pytest.mark.parametrize("bad_slug", ["credit_card", "investment"])
def test_non_asset_source_rejected(session_factory, worlds, bad_slug):
    a = worlds["a"]
    bad_source_id = a["cc_id"] if bad_slug == "credit_card" else a["investment_id"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{a['savings_id']}",
            json={"payment_source_account_id": bad_source_id},
        )
    assert res.status_code == 422, res.text


# ── 4: self-pay prevention ─────────────────────────────────────────────────


def test_self_pay_rejected(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{a['checking_id']}",
            json={"payment_source_account_id": a["checking_id"]},
        )
    assert res.status_code == 422, res.text


# ── 5: deletion + deactivation ─────────────────────────────────────────────


def test_deleting_source_sets_target_pointer_null(session_factory, worlds):
    """FK ON DELETE SET NULL: after the source account is deleted, the
    target's payment_source_account_id must be NULL, not dangling."""
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        set_res = client.put(
            f"/api/v1/accounts/{a['cc_id']}",
            json={"payment_source_account_id": a["savings_id"]},
        )
        assert set_res.status_code == 200, set_res.text

        del_res = client.delete(f"/api/v1/accounts/{a['savings_id']}")
        assert del_res.status_code == 204, del_res.text

    import asyncio

    target = asyncio.get_event_loop().run_until_complete(
        _account_row(session_factory, a["cc_id"])
    )
    assert target.payment_source_account_id is None


def test_inactive_source_rejected_at_write_time(session_factory, worlds):
    a = worlds["a"]
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{a['cc_id']}",
            json={"payment_source_account_id": a["inactive_checking_id"]},
        )
    assert res.status_code == 422, res.text
