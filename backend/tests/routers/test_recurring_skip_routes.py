"""Wire tests for skip-next / materialise-next / skip-an-occurrence
(TBD-272, TBD-273).

The behaviour is fenced in ``tests/services/test_recurring_skip_and_edit_next.py``.
This file fences what that suite cannot see: the routes exist, are org-scoped,
map each guard to its status code, and ship a real ``TransactionResponse``.
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
                await db.execute(select(User).where(User.username == "root"))
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
    app.include_router(transactions_router)
    return app


async def _org(db: AsyncSession, name: str, username: str | None) -> dict:
    org = Organization(name=name, billing_cycle_day=1)
    db.add(org)
    await db.flush()
    if username:
        db.add(User(
            org_id=org.id, username=username, email=f"{username}@example.com",
            password_hash=hash_password("pw-1234567"), role=Role.OWNER,
            is_active=True, email_verified=True,
        ))
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db.add(at)
    await db.flush()
    acct = Account(
        org_id=org.id, name="A", account_type_id=at.id,
        balance=Decimal("1000"), currency="EUR",
    )
    cat = Category(org_id=org.id, name="Rent", slug="rent", type=CategoryType.EXPENSE)
    db.add_all([acct, cat])
    await db.flush()
    return {"org_id": org.id, "acct_id": acct.id, "cat_id": cat.id}


async def _template(db: AsyncSession, seed: dict, **kw) -> RecurringTransaction:
    fields = dict(
        org_id=seed["org_id"], account_id=seed["acct_id"], category_id=seed["cat_id"],
        description="Rent", amount=Decimal("37.00"), type=TransactionType.EXPENSE,
        frequency=Frequency.MONTHLY, next_due_date=date.today() + timedelta(days=3),
        auto_settle=False, is_active=True, occurrences_elapsed=0,
    )
    fields.update(kw)
    r = RecurringTransaction(**fields)
    db.add(r)
    await db.flush()
    return r


@pytest.mark.parametrize(
    ("path", "reverted"), [("skip-next", True), ("materialise-next", False)],
)
async def test_frontier_routes_return_the_created_row(session_factory, path, reverted):
    """FENCE. Kills a route returning the template, or a bare dict: the body is
    a ``TransactionResponse`` for the row at the old frontier."""
    async with session_factory() as db:
        seed = await _org(db, "Mine", "root")
        r = await _template(db, seed)
        await db.commit()
        rid, due = r.id, r.next_due_date

    resp = TestClient(make_app(session_factory)).post(f"/api/v1/recurring/{rid}/{path}")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["recurring_id"], body["date"], body["status"]) == (rid, due.isoformat(), "pending")
    assert body["is_reverted"] is reverted
    assert body["differs_from_series"] is False


@pytest.mark.parametrize("case", ["at_frontier", "inactive", "exhausted", "before_cycle_start"])
async def test_frontier_route_guards_are_409(session_factory, case):
    """GUARD. Each refusal maps to 409 with a detail, and nothing is written
    beyond what the fixture put there."""
    today = date.today()
    async with session_factory() as db:
        seed = await _org(db, "Mine", "root")
        kw = {
            "at_frontier": {},
            "inactive": {"is_active": False},
            "exhausted": {"occurrence_count": 1, "occurrences_elapsed": 1},
            "before_cycle_start": {"next_due_date": today.replace(day=1) - timedelta(days=2)},
        }[case]
        r = await _template(db, seed, **kw)
        if case == "at_frontier":
            db.add(Transaction(
                org_id=seed["org_id"], account_id=seed["acct_id"], category_id=seed["cat_id"],
                description="Rent", amount=Decimal("37.00"), type=TransactionType.EXPENSE,
                status=TransactionStatus.PENDING, date=r.next_due_date, recurring_id=r.id,
            ))
        await db.commit()
        rid = r.id

    resp = TestClient(make_app(session_factory)).post(f"/api/v1/recurring/{rid}/skip-next")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]
    async with session_factory() as db:
        count = len((await db.execute(select(Transaction.id))).all())
    assert count == (1 if case == "at_frontier" else 0)


async def test_routes_are_org_scoped(session_factory):
    """FENCE. Kills a lookup by id alone: another org's template and row are 404."""
    async with session_factory() as db:
        mine = await _org(db, "Mine", "root")
        own = await _template(db, mine)
        theirs = await _org(db, "Theirs", None)
        r = await _template(db, theirs)
        tx = Transaction(
            org_id=theirs["org_id"], account_id=theirs["acct_id"], category_id=theirs["cat_id"],
            description="Rent", amount=Decimal("37.00"), type=TransactionType.EXPENSE,
            status=TransactionStatus.PENDING, date=date.today(), recurring_id=r.id,
        )
        db.add(tx)
        await db.commit()
        rid, tid, own_id = r.id, tx.id, own.id

    client = TestClient(make_app(session_factory))
    # Anti-vacuity: the route exists, so the 404s below are the org scope.
    assert client.post(f"/api/v1/recurring/{own_id}/materialise-next").status_code == 201
    assert client.post(f"/api/v1/recurring/{rid}/skip-next").status_code == 404
    assert client.post(f"/api/v1/recurring/{rid}/materialise-next").status_code == 404
    assert client.post(f"/api/v1/transactions/{tid}/skip").status_code == 404


async def test_skip_occurrence_route(session_factory):
    """FENCE. 200 with the skipped row; a second skip is a 409."""
    async with session_factory() as db:
        seed = await _org(db, "Mine", "root")
        r = await _template(db, seed)
        tx = Transaction(
            org_id=seed["org_id"], account_id=seed["acct_id"], category_id=seed["cat_id"],
            description="Rent", amount=Decimal("37.00"), type=TransactionType.EXPENSE,
            status=TransactionStatus.PENDING, date=date.today(), recurring_id=r.id,
        )
        db.add(tx)
        await db.commit()
        tid = tx.id

    client = TestClient(make_app(session_factory))
    resp = client.post(f"/api/v1/transactions/{tid}/skip")
    assert resp.status_code == 200, resp.text
    assert (resp.json()["id"], resp.json()["is_reverted"]) == (tid, True)
    again = client.post(f"/api/v1/transactions/{tid}/skip")
    assert again.status_code == 409
