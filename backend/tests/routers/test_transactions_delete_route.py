"""Router-level tests for DELETE /api/v1/transactions/{id} (TBD-294).

The service-level behaviour -- which rows are demoted, what happens to
balances, the import-batch counters -- is covered by
``tests/services/test_matched_row_actions.py``. This file exists because that
suite cannot see the WIRE, and the wire changed: the endpoint used to be
``status_code=204`` with no body and is now a 200 carrying
``DeleteTransactionResponse``.

Nothing in ``backend/tests/`` exercised this endpoint through HTTP at all
before, so restoring the 204 -- or quietly dropping ``demoted_ids`` from the
response model -- was invisible to the whole suite while the frontend reads
``res.demoted_ids`` to decide whether to warn the user that an irreversible
demotion just happened.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
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
from app.models.transaction import TransactionStatus, TransactionType
from app.models.user import Role, User
from app.routers.transactions import router as transactions_router
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
    # ``transactions.linked_transaction_id`` and the delete fails on the FK
    # instead of producing the orphan this endpoint reports.
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

    app.include_router(transactions_router)
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
            org_id=org.id, name="Groceries", slug="groceries",
            type=CategoryType.EXPENSE, is_system=False,
        )
        db.add(cat)
        await db.commit()
        return {"org_id": org.id, "acct_id": acct.id, "cat_id": cat.id}


async def _add_tx(
    factory, seed: dict, *, amount: str, linked_transaction_id: int | None = None
) -> int:
    async with factory() as db:
        tx = Transaction(
            org_id=seed["org_id"], account_id=seed["acct_id"],
            category_id=seed["cat_id"], description=f"row-{amount}",
            amount=Decimal(amount), type=TransactionType.EXPENSE,
            status=TransactionStatus.SETTLED, date=date(2026, 5, 1),
            settled_date=date(2026, 5, 1),
            linked_transaction_id=linked_transaction_id,
        )
        db.add(tx)
        await db.commit()
        return tx.id


@pytest.mark.asyncio
async def test_delete_returns_200_with_demoted_ids(session_factory):
    """R1. The wire contract.

    Kills BOTH: restoring ``status_code=204`` on the route (a 204 carries no
    body, so the frontend's ``res.demoted_ids`` is ``undefined`` and the
    irreversible demotion goes unannounced), and dropping ``demoted_ids``
    from ``DeleteTransactionResponse``.
    """
    seed = await _seed(session_factory)
    canonical_id = await _add_tx(session_factory, seed, amount="90.00")
    # ONE-WAY link: a reconcile match, not a transfer pair.
    dup_id = await _add_tx(
        session_factory, seed, amount="90.00",
        linked_transaction_id=canonical_id,
    )

    client = TestClient(make_app(session_factory))
    resp = client.delete(f"/api/v1/transactions/{canonical_id}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] is True
    assert body["demoted_ids"] == [dup_id]

    async with session_factory() as db:
        dup = await db.scalar(select(Transaction).where(Transaction.id == dup_id))
        assert dup is not None
        assert dup.reconciliation_state == "rejected"
        assert dup.linked_transaction_id is None


@pytest.mark.asyncio
async def test_delete_with_nothing_to_demote_still_returns_a_body(session_factory):
    """R2. The ordinary case must carry the SAME shape, not a 204 and not a
    bare ``null`` -- the client reads ``demoted_ids`` unconditionally."""
    seed = await _seed(session_factory)
    tx_id = await _add_tx(session_factory, seed, amount="7.00")

    client = TestClient(make_app(session_factory))
    resp = client.delete(f"/api/v1/transactions/{tx_id}")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": True, "demoted_ids": []}


@pytest.mark.asyncio
async def test_delete_missing_transaction_is_404(session_factory):
    """R3. The status mapping survives the 204 -> 200 change."""
    await _seed(session_factory)
    client = TestClient(make_app(session_factory))
    assert client.delete("/api/v1/transactions/424242").status_code == 404
