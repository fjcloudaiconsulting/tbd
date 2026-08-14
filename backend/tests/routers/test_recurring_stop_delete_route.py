"""Router-level tests for POST /api/v1/recurring/{id}/stop and
DELETE /api/v1/recurring/{id} (TBD-312).

The service-level behaviour -- which rows are demoted, what happens to
balances and the import-batch counters -- is covered by
``tests/services/test_matched_row_actions.py``. This file exists because that
suite cannot see the WIRE, and **nothing in backend/tests exercised either of
these endpoints through HTTP at all** before TBD-312.

That gap is the whole ticket. Both routes declared ``response_model=dict``,
which validates nothing and documents nothing, and returned only
``{"stopped"/"deleted": True, "pending_removed": n}``. So stopping a template
could irreversibly mark a matched duplicate REJECTED -- removing its amount
from every balance and every report, permanently, recoverable only by direct
SQL -- while telling the user only that pending rows were removed. Its
sibling ``DELETE /transactions/{id}`` has reported ``demoted_ids`` since
TBD-294.

Each test names the wrong implementation it kills.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user
from app.models import Account, AccountType, Category, Organization, Transaction
from app.models.base import Base
from app.models.category import CategoryType
from app.models.recurring import Frequency, RecurringTransaction
from app.models.transaction import TransactionStatus, TransactionType
from app.models.user import Role, User
from app.routers.recurring import router as recurring_router
from app.security import hash_password
from app.services.exceptions import ConflictError, NotFoundError, ValidationError


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # LOAD-BEARING: without it SQLite ignores ``ON DELETE SET NULL`` on
    # ``transactions.linked_transaction_id``, the orphan these routes must
    # report is never produced, and the fences pass for the wrong reason.
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


def make_app(session_factory) -> FastAPI:
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user() -> User:
        async with session_factory() as db:
            return (
                await db.execute(select(User).where(User.is_superadmin.is_(True)))
            ).scalar_one()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user

    @app.exception_handler(NotFoundError)
    async def _nf(_req, exc: NotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    async def _ve(_req, exc: ValidationError):
        return JSONResponse(status_code=400, content={"detail": exc.detail})

    @app.exception_handler(ConflictError)
    async def _ce(_req, exc: ConflictError):
        return JSONResponse(status_code=409, content={"detail": exc.detail})

    app.include_router(recurring_router)
    return app


async def _seed(factory) -> dict:
    async with factory() as db:
        org = Organization(name="Test Org", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id, username="root", email="root@example.com",
            password_hash=hash_password("pw-1234567"), role=Role.OWNER,
            is_superadmin=True, is_active=True, email_verified=True,
        )
        at = AccountType(
            org_id=org.id, name="Checking", slug="checking", is_system=True
        )
        db.add_all([user, at])
        await db.flush()
        acct = Account(
            org_id=org.id, name="Acct A", account_type_id=at.id,
            balance=Decimal("1000"), opening_balance=Decimal("1000"),
            opening_balance_date=date(2026, 1, 1), currency="EUR",
        )
        db.add(acct)
        await db.flush()
        cat = Category(
            org_id=org.id, name="Rent", slug="rent",
            type=CategoryType.EXPENSE, is_system=False,
        )
        db.add(cat)
        await db.commit()
        return {"org_id": org.id, "acct_id": acct.id, "cat_id": cat.id}


async def _make_template_with_pending_and_match(factory, seed: dict) -> dict:
    """A recurring template with one FUTURE PENDING row, and a settled bank
    row matched against it one-way.

    This is the natural shape, not an exotic one: matching an imported bank
    charge against the pending recurring row it settles is one of the most
    ordinary actions in the product, and ``_apply_match`` validates its target
    on org, existence and not-self only -- no status filter, no
    ``recurring_id`` filter.
    """
    async with factory() as db:
        future = date.today() + timedelta(days=7)
        rec = RecurringTransaction(
            org_id=seed["org_id"], account_id=seed["acct_id"],
            category_id=seed["cat_id"], description="Rent",
            amount=Decimal("1200.00"), type=TransactionType.EXPENSE,
            frequency=Frequency.MONTHLY, next_due_date=future,
            auto_settle=False, is_active=True,
        )
        db.add(rec)
        await db.flush()

        pending = Transaction(
            org_id=seed["org_id"], account_id=seed["acct_id"],
            category_id=seed["cat_id"], description="Rent (pending)",
            amount=Decimal("1200.00"), type=TransactionType.EXPENSE,
            status=TransactionStatus.PENDING, date=future,
            recurring_id=rec.id,
        )
        db.add(pending)
        await db.flush()

        # The matched duplicate: points AT the pending row one-way, and its
        # own contribution has already been reverted, which is exactly why
        # deleting its target must demote it rather than silently release it
        # back into balances and reports.
        dup = Transaction(
            org_id=seed["org_id"], account_id=seed["acct_id"],
            category_id=seed["cat_id"], description="Rent (bank)",
            amount=Decimal("1200.00"), type=TransactionType.EXPENSE,
            status=TransactionStatus.SETTLED, date=date.today(),
            settled_date=date.today(),
            linked_transaction_id=pending.id,
            reconciliation_state="matched",
        )
        db.add(dup)
        await db.commit()
        return {"rec_id": rec.id, "pending_id": pending.id, "dup_id": dup.id}


@pytest.mark.asyncio
async def test_stop_returns_demoted_ids(session_factory):
    """R1. THE TICKET, stop half.

    Kills: returning ``{"stopped": True, "pending_removed": n}`` and nothing
    else. The user stops a rent template, a matched duplicate is irreversibly
    marked REJECTED, and the response says only that one pending row went.
    """
    seed = await _seed(session_factory)
    ids = await _make_template_with_pending_and_match(session_factory, seed)

    with TestClient(make_app(session_factory)) as client:
        resp = client.post(f"/api/v1/recurring/{ids['rec_id']}/stop")

    assert resp.status_code == 200
    body = resp.json()
    assert body["stopped"] is True
    assert body["pending_removed"] == 1
    assert body["demoted_ids"] == [ids["dup_id"]]


@pytest.mark.asyncio
async def test_delete_returns_demoted_ids(session_factory):
    """R2. THE TICKET, delete half. Fenced SEPARATELY on purpose.

    Both routes reach the demotion helper by different paths, and this repo
    has repeatedly shipped a fix to one sibling and not the other. Wiring
    ``demoted_ids`` into ``stop`` alone would leave this green.
    """
    seed = await _seed(session_factory)
    ids = await _make_template_with_pending_and_match(session_factory, seed)

    with TestClient(make_app(session_factory)) as client:
        resp = client.delete(f"/api/v1/recurring/{ids['rec_id']}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] is True
    assert body["pending_removed"] == 1
    assert body["demoted_ids"] == [ids["dup_id"]]


@pytest.mark.asyncio
async def test_stop_with_nothing_demoted_still_returns_the_field(session_factory):
    """R3. The empty-case control.

    Kills a route that only includes ``demoted_ids`` when it is non-empty, or
    a response model that omits it by default. The frontend branches on the
    field, so it must always be present and always be a list -- and it must
    NOT name ids that were never rejected.
    """
    seed = await _seed(session_factory)
    async with session_factory() as db:
        future = date.today() + timedelta(days=7)
        rec = RecurringTransaction(
            org_id=seed["org_id"], account_id=seed["acct_id"],
            category_id=seed["cat_id"], description="Gym",
            amount=Decimal("30.00"), type=TransactionType.EXPENSE,
            frequency=Frequency.MONTHLY, next_due_date=future,
            auto_settle=False, is_active=True,
        )
        db.add(rec)
        await db.commit()
        rec_id = rec.id

    with TestClient(make_app(session_factory)) as client:
        resp = client.post(f"/api/v1/recurring/{rec_id}/stop")

    assert resp.status_code == 200
    assert resp.json() == {
        "stopped": True,
        "pending_removed": 0,
        "demoted_ids": [],
    }


@pytest.mark.asyncio
async def test_stop_missing_template_is_404(session_factory):
    """R4. Control: the not-found path still behaves, and the new response
    model did not swallow the error handler."""
    await _seed(session_factory)
    with TestClient(make_app(session_factory)) as client:
        resp = client.post("/api/v1/recurring/999999/stop")
    assert resp.status_code == 404
