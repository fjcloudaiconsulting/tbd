"""Opening balance on accounts (L3.2 Wave 2A).

Covers the API surface that PR #227 locked in
``specs/2026-05-12-l3-2-import-contracts.md`` §0.4 / §4 / §4.4:

- ``POST /api/v1/accounts`` accepts optional ``opening_balance`` and
  ``opening_balance_date``; both default to 0 / today when omitted.
- ``PUT /api/v1/accounts/{id}`` edits both fields; an actual change
  writes an ``account.opening_balance.update`` audit row with the
  old + new values. A no-op PUT (same values, or fields omitted)
  writes no audit row.
- ``GET /api/v1/accounts/{id}`` exposes both fields on the response.

Backend stack: FastAPI + SQLAlchemy 2.0 async, in-memory aiosqlite
mirroring the test_account_balance_adjustment.py setup.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
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
from app.models.audit_event import AuditEvent
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


@pytest_asyncio.fixture
async def seeded(session_factory) -> dict:
    """Seed an org with an admin user, an account type, and one account
    that the tests then PUT against. ``account.opening_balance`` is
    initialised to 0 to mirror the migration's CANONICAL backfill."""
    async with session_factory() as db:
        org = Organization(name="OB Test Org", billing_cycle_day=1)
        db.add(org)
        await db.flush()

        admin = User(
            org_id=org.id,
            username="admin",
            email="admin@ob.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.ADMIN,
            is_active=True,
            email_verified=True,
        )
        db.add(admin)

        at = AccountType(
            org_id=org.id, name="Checking", slug="checking", is_system=True
        )
        db.add(at)
        await db.flush()

        acct = Account(
            org_id=org.id,
            account_type_id=at.id,
            name="Primary",
            balance=Decimal("0.00"),
            currency="EUR",
            is_active=True,
            opening_balance=Decimal("0.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        db.add(acct)
        await db.commit()

        return {
            "org_id": org.id,
            "admin_id": admin.id,
            "account_type_id": at.id,
            "account_id": acct.id,
        }


def _make_app(session_factory) -> FastAPI:
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_session_factory():
        return session_factory

    async def override_current_user() -> User:
        async with session_factory() as db:
            return (
                await db.execute(select(User).where(User.role == Role.ADMIN))
            ).scalar_one()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.dependency_overrides[get_current_user] = override_current_user
    app.include_router(accounts_router)
    return app


# ── Create ────────────────────────────────────────────────────────────────


def test_create_account_defaults_opening_balance_to_zero(session_factory, seeded):
    """Caller omits opening_balance — server defaults to 0."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/accounts",
            json={
                "name": "Secondary",
                "account_type_id": seeded["account_type_id"],
                "currency": "EUR",
            },
        )
    assert res.status_code == 201, res.text
    body = res.json()
    assert Decimal(body["opening_balance"]) == Decimal("0.00")
    # opening_balance_date is set by the DB default — on SQLite that
    # resolves to today. We only assert it's present and parseable.
    assert body["opening_balance_date"] is not None
    date.fromisoformat(body["opening_balance_date"])


def test_create_account_with_explicit_opening_balance(session_factory, seeded):
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/accounts",
            json={
                "name": "Savings",
                "account_type_id": seeded["account_type_id"],
                "currency": "EUR",
                "opening_balance": "1500.00",
                "opening_balance_date": "2025-12-31",
            },
        )
    assert res.status_code == 201, res.text
    body = res.json()
    assert Decimal(body["opening_balance"]) == Decimal("1500.00")
    assert body["opening_balance_date"] == "2025-12-31"


def test_create_account_rejects_oversized_opening_balance(session_factory, seeded):
    """Schema-level guard keeps a Numeric(12, 2) overflow off the DB layer."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/accounts",
            json={
                "name": "Big",
                "account_type_id": seeded["account_type_id"],
                "currency": "EUR",
                "opening_balance": "99999999999999.99",
            },
        )
    assert res.status_code == 422


# ── Read ──────────────────────────────────────────────────────────────────


def test_get_account_exposes_opening_balance_fields(session_factory, seeded):
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.get(f"/api/v1/accounts/{seeded['account_id']}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert "opening_balance" in body
    assert "opening_balance_date" in body
    assert Decimal(body["opening_balance"]) == Decimal("0.00")
    assert body["opening_balance_date"] == "2026-01-01"


# ── Update ────────────────────────────────────────────────────────────────


def test_update_account_opening_balance_writes_audit_row(session_factory, seeded):
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{seeded['account_id']}",
            json={
                "opening_balance": "250.50",
                "opening_balance_date": "2025-06-15",
            },
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert Decimal(body["opening_balance"]) == Decimal("250.50")
    assert body["opening_balance_date"] == "2025-06-15"

    # Audit event written in its own session (record_audit_event path).
    import asyncio

    async def _audit_rows():
        async with session_factory() as db:
            return (
                await db.execute(
                    select(AuditEvent).where(
                        AuditEvent.event_type == "account.opening_balance.update"
                    )
                )
            ).scalars().all()

    rows = asyncio.get_event_loop().run_until_complete(_audit_rows())
    assert len(rows) == 1, "expected exactly one audit event"
    detail = rows[0].detail
    assert detail["account_id"] == seeded["account_id"]
    assert detail["old_opening_balance"] == "0.00"
    assert detail["new_opening_balance"] == "250.50"
    assert detail["old_opening_balance_date"] == "2026-01-01"
    assert detail["new_opening_balance_date"] == "2025-06-15"


def test_update_opening_balance_shifts_live_balance_by_delta(session_factory, seeded):
    """Editing opening_balance must move the live balance by the same delta so
    the invariant ``balance == opening_balance + Σ settled txns`` holds.
    Regression for the 2026-06-14 drift: opening edits left ``balance`` stale,
    silently de/inflating the account."""
    import asyncio

    # Simulate an account that already carries transactions:
    # balance(500) = opening(100) + 400 of net settled activity.
    async def _prime():
        async with session_factory() as db:
            acct = await db.get(Account, seeded["account_id"])
            acct.opening_balance = Decimal("100.00")
            acct.balance = Decimal("500.00")
            await db.commit()

    asyncio.get_event_loop().run_until_complete(_prime())

    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{seeded['account_id']}",
            json={"opening_balance": "300.00"},
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert Decimal(body["opening_balance"]) == Decimal("300.00")
    # delta = 300 - 100 = +200 → balance 500 -> 700; the +400 txn component is preserved.
    assert Decimal(body["balance"]) == Decimal("700.00")


def test_update_account_opening_balance_no_change_no_audit(session_factory, seeded):
    """Submitting the same values is a no-op — no audit row."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{seeded['account_id']}",
            json={
                "opening_balance": "0.00",
                "opening_balance_date": "2026-01-01",
            },
        )
    assert res.status_code == 200, res.text

    import asyncio

    async def _audit_rows():
        async with session_factory() as db:
            return (
                await db.execute(
                    select(AuditEvent).where(
                        AuditEvent.event_type == "account.opening_balance.update"
                    )
                )
            ).scalars().all()

    rows = asyncio.get_event_loop().run_until_complete(_audit_rows())
    assert rows == []


def test_update_account_omitting_opening_fields_does_not_touch_them(
    session_factory, seeded
):
    """A PUT with only ``name`` set must leave opening_balance unchanged
    and emit no opening-balance audit row."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{seeded['account_id']}",
            json={"name": "Renamed"},
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["name"] == "Renamed"
    assert Decimal(body["opening_balance"]) == Decimal("0.00")
    assert body["opening_balance_date"] == "2026-01-01"

    import asyncio

    async def _audit_rows():
        async with session_factory() as db:
            return (
                await db.execute(
                    select(AuditEvent).where(
                        AuditEvent.event_type == "account.opening_balance.update"
                    )
                )
            ).scalars().all()

    rows = asyncio.get_event_loop().run_until_complete(_audit_rows())
    assert rows == []


def test_update_account_rejects_oversized_opening_balance(session_factory, seeded):
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{seeded['account_id']}",
            json={"opening_balance": "999999999999.99"},
        )
    assert res.status_code == 422


# ── Deactivate guard vs. opening-balance shift (finding 15) ─────────────────


def test_deactivate_blocked_when_opening_shift_makes_balance_nonzero(
    session_factory, seeded
):
    """A single PUT that sets opening_balance > 0 AND is_active=false must be
    REJECTED: the post-shift balance is nonzero, so the account is not empty.
    Under the old field order the guard ran before the shift, saw the
    pre-shift balance (0), and wrongly let the deactivation through."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{seeded['account_id']}",
            json={"opening_balance": "500.00", "is_active": False},
        )
    assert res.status_code == 409, res.text

    # The whole PUT must roll back atomically: opening_balance and is_active
    # both unchanged, balance still 0.
    import asyncio

    async def _reload():
        async with session_factory() as db:
            return await db.get(Account, seeded["account_id"])

    acct = asyncio.get_event_loop().run_until_complete(_reload())
    assert acct.is_active is True
    assert acct.balance == Decimal("0.00")
    assert acct.opening_balance == Decimal("0.00")


def test_deactivate_allowed_when_opening_shift_zeroes_balance(
    session_factory, seeded
):
    """A single PUT that lowers opening_balance to 0 (zeroing the live balance)
    AND sets is_active=false must be ALLOWED: the post-shift balance is 0.
    Under the old order the guard saw the pre-shift balance (500) and wrongly
    raised 409."""
    import asyncio

    # Prime: balance(500) == opening(500) + 0 settled activity.
    async def _prime():
        async with session_factory() as db:
            acct = await db.get(Account, seeded["account_id"])
            acct.opening_balance = Decimal("500.00")
            acct.balance = Decimal("500.00")
            await db.commit()

    asyncio.get_event_loop().run_until_complete(_prime())

    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{seeded['account_id']}",
            json={"opening_balance": "0.00", "is_active": False},
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["is_active"] is False
    assert Decimal(body["balance"]) == Decimal("0.00")
    assert Decimal(body["opening_balance"]) == Decimal("0.00")


def test_deactivate_only_still_blocks_nonzero_account(session_factory, seeded):
    """Guard regression: an is_active-only PUT (no opening edit) must still 409
    when the account holds a nonzero balance — the shift is a no-op so the
    guard sees the unchanged balance."""
    import asyncio

    async def _prime():
        async with session_factory() as db:
            acct = await db.get(Account, seeded["account_id"])
            acct.opening_balance = Decimal("100.00")
            acct.balance = Decimal("300.00")
            await db.commit()

    asyncio.get_event_loop().run_until_complete(_prime())

    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{seeded['account_id']}",
            json={"is_active": False},
        )
    assert res.status_code == 409, res.text


# ── End-to-end: both fixed paths uphold the invariant (finding 16) ──────────


def test_opening_edit_shifts_balance_and_reconcile_stays_consistent(
    session_factory, seeded
):
    """End-to-end coupling of both fixes: with real settled transactions giving
    ``balance == opening + Σtxns``, editing opening_balance shifts the live
    balance by the delta AND reconcile_account reports stored == computed."""
    import asyncio

    from app.schemas.transaction import TransactionCreate
    from app.services import transaction_service as ts
    from app.models import Category
    from app.models.category import CategoryType

    async def _prime():
        async with session_factory() as db:
            acct = await db.get(Account, seeded["account_id"])
            acct.opening_balance = Decimal("1000.00")
            acct.balance = Decimal("1000.00")
            cat = Category(
                org_id=seeded["org_id"], name="Gen", slug="gen",
                type=CategoryType.BOTH, is_system=True,
            )
            db.add(cat)
            await db.flush()
            cat_id = cat.id
            await ts.create_transaction(db, seeded["org_id"], TransactionCreate(
                account_id=acct.id, category_id=cat_id, description="pay",
                amount=Decimal("200.00"), type="income", status="settled",
                date=date(2026, 6, 1)))
            await ts.create_transaction(db, seeded["org_id"], TransactionCreate(
                account_id=acct.id, category_id=cat_id, description="buy",
                amount=Decimal("50.00"), type="expense", status="settled",
                date=date(2026, 6, 2)))
            await db.commit()

    asyncio.get_event_loop().run_until_complete(_prime())

    # balance now = 1000 + 200 - 50 = 1150.
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{seeded['account_id']}",
            json={"opening_balance": "1300.00"},
        )
    assert res.status_code == 200, res.text
    body = res.json()
    # delta = 1300 - 1000 = +300 → balance 1150 -> 1450.
    assert Decimal(body["balance"]) == Decimal("1450.00")

    from app.services import transaction_service as ts2

    async def _reconcile():
        async with session_factory() as db:
            acct = await db.get(Account, seeded["account_id"])
            return await ts2.reconcile_account(db, seeded["org_id"], acct)

    stored, computed, consistent = asyncio.get_event_loop().run_until_complete(
        _reconcile()
    )
    assert stored == Decimal("1450.00")
    assert computed == Decimal("1450.00")
    assert consistent is True
