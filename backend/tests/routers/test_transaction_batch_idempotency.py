"""TBD-784: ``POST /api/v1/transactions/batch`` honours an ``Idempotency-Key``.

* A repeat of the same key by the same user returns the first per-row result
  with ``replayed=true`` and writes nothing (no rows, no balance change, no
  second audit event).
* A different key, a different user, or a key older than ``KEY_TTL_HOURS``
  writes as normal.
* The same key with a different body is a 409 and writes nothing.
* No key keeps the old behaviour; a malformed key is a 422.
* The retention job deletes only expired keys.
"""
from __future__ import annotations

import datetime
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app._time import utcnow_naive
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.account import Account, AccountType
from app.models.audit_event import AuditEvent
from app.models.category import Category, CategoryType
from app.models.transaction import Transaction
from app.models.transaction_batch_key import TransactionBatchKey
from app.models.user import Organization, Role, User
from app.routers.transactions import router as transactions_router
from app.services.exceptions import ConflictError
from app.services.scheduler.jobs.batch_key_purge import run_batch_key_purge
from app.services.transaction_batch_service import KEY_TTL_HOURS

KEY = "3f2b8a1c-5d4e-4f60-9a7b-8c9d0e1f2a3b"


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _seed_org(factory, name: str) -> dict:
    async with factory() as db:
        org = Organization(name=name, billing_cycle_day=1)
        db.add(org)
        await db.flush()
        users = [
            User(org_id=org.id, username=f"{name}{i}", email=f"{name}{i}@test.example",
                 password_hash="x", role=Role.OWNER, is_active=True, email_verified=True)
            for i in (1, 2)
        ]
        atype = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
        db.add_all([*users, atype])
        await db.flush()
        account = Account(org_id=org.id, account_type_id=atype.id, name="Cash",
                          balance=Decimal("100.00"), currency="EUR")
        category = Category(org_id=org.id, name="Food", slug="food", type=CategoryType.EXPENSE)
        db.add_all([account, category])
        await db.commit()
        return {"org_id": org.id, "user_ids": [u.id for u in users],
                "account_id": account.id, "category_id": category.id}


@pytest_asyncio.fixture
async def seed(factory) -> dict:
    return await _seed_org(factory, "alpha")


def _app(factory) -> FastAPI:
    app = FastAPI()
    app.state.user_id = None

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    async def override_user() -> User:
        async with factory() as db:
            return await db.get(User, app.state.user_id)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_session_factory] = lambda: factory

    @app.exception_handler(ConflictError)
    async def _conflict(request, exc):
        return JSONResponse(status_code=409, content={"detail": exc.detail})

    app.include_router(transactions_router)
    return app


def _body(seed: dict, amounts=("12.50", "7.25")) -> dict:
    rows = [
        {"row_number": i + 1, "transaction": {
            "account_id": seed["account_id"], "category_id": seed["category_id"],
            "description": f"Row {i + 1}", "amount": amount, "type": "expense",
            "date": "2026-11-01",
        }}
        for i, amount in enumerate(amounts)
    ]
    # One bad row, so the replayed per-row errors are checked too.
    rows.append({"row_number": len(rows) + 1, "transaction": {
        "account_id": 999999, "category_id": seed["category_id"],
        "description": "Bad", "amount": "1.00", "type": "expense", "date": "2026-11-01",
    }})
    return {"rows": rows}


def _post(client: TestClient, app: FastAPI, user_id: int, body: dict, key: str | None):
    app.state.user_id = user_id
    headers = {"Idempotency-Key": key} if key else {}
    return client.post("/api/v1/transactions/batch", json=body, headers=headers)


async def _state(factory, seed: dict) -> tuple[int, Decimal, int]:
    async with factory() as db:
        tx = await db.scalar(select(func.count()).select_from(Transaction)
                             .where(Transaction.org_id == seed["org_id"]))
        balance = await db.scalar(select(Account.balance)
                                  .where(Account.id == seed["account_id"]))
        audits = await db.scalar(select(func.count()).select_from(AuditEvent)
                                 .where(AuditEvent.event_type == "transactions.batch_create"))
    return tx, balance, audits


async def test_replay_returns_the_first_result_and_writes_nothing(factory, seed):
    """FENCE. Wrong implementations: no key handling (rows written twice),
    a replay that re-runs the rows, or one that drops the per-row errors."""
    app = _app(factory)
    user = seed["user_ids"][0]
    with TestClient(app) as client:
        first = _post(client, app, user, _body(seed), KEY)
        after_first = await _state(factory, seed)
        second = _post(client, app, user, _body(seed), KEY)

    assert first.status_code == second.status_code == 200
    a, b = first.json(), second.json()
    assert a["replayed"] is False and b["replayed"] is True
    assert a["imported_count"] == 2 and a["error_count"] == 1
    assert {k: v for k, v in b.items() if k != "replayed"} == {
        k: v for k, v in a.items() if k != "replayed"
    }
    assert after_first == (2, Decimal("80.25"), 1)
    assert await _state(factory, seed) == after_first


async def test_a_different_key_writes_again(factory, seed):
    app = _app(factory)
    user = seed["user_ids"][0]
    with TestClient(app) as client:
        first = _post(client, app, user, _body(seed), KEY)
        second = _post(client, app, user, _body(seed), "another-key-0123456789")
    assert second.json()["replayed"] is False
    assert {r["transaction_id"] for r in first.json()["results"]}.isdisjoint(
        r["transaction_id"] for r in second.json()["results"]
    )
    assert (await _state(factory, seed))[0] == 4


async def test_no_key_keeps_writing_every_time(factory, seed):
    app = _app(factory)
    with TestClient(app) as client:
        for _ in range(2):
            assert _post(client, app, seed["user_ids"][0], _body(seed), None).status_code == 200
    assert (await _state(factory, seed))[0] == 4


async def test_same_key_with_another_body_is_a_409_and_writes_nothing(factory, seed):
    """FENCE. Wrong implementation: replaying the first result for a request
    the client never made, or writing the new rows under a used key."""
    app = _app(factory)
    user = seed["user_ids"][0]
    with TestClient(app) as client:
        _post(client, app, user, _body(seed), KEY)
        before = await _state(factory, seed)
        resp = _post(client, app, user, _body(seed, amounts=("99.00",)), KEY)
    assert resp.status_code == 409
    assert await _state(factory, seed) == before


async def test_key_is_scoped_to_the_user_and_the_org(factory, seed):
    """FENCE. Wrong implementations: a key unique per org only (the second
    user is answered with the first user's rows) or global (another org is)."""
    other = await _seed_org(factory, "beta")
    app = _app(factory)
    with TestClient(app) as client:
        _post(client, app, seed["user_ids"][0], _body(seed), KEY)
        same_org = _post(client, app, seed["user_ids"][1], _body(seed), KEY)
        other_org = _post(client, app, other["user_ids"][0], _body(other), KEY)
    assert same_org.json()["replayed"] is False
    assert other_org.json()["replayed"] is False
    assert (await _state(factory, seed))[0] == 4
    assert (await _state(factory, other))[0] == 2


async def test_an_expired_key_writes_as_new(factory, seed):
    """FENCE. Wrong implementations: a key that never expires, or an expired
    row left in place so the new claim hits the unique index."""
    app = _app(factory)
    user = seed["user_ids"][0]
    with TestClient(app) as client:
        _post(client, app, user, _body(seed), KEY)
        async with factory() as db:
            await db.execute(update(TransactionBatchKey).values(
                created_at=utcnow_naive() - datetime.timedelta(hours=KEY_TTL_HOURS, minutes=1)
            ))
            await db.commit()
        again = _post(client, app, user, _body(seed), KEY)
        replay = _post(client, app, user, _body(seed), KEY)
    assert again.json()["replayed"] is False
    assert replay.json()["replayed"] is True
    assert replay.json()["results"] == again.json()["results"]
    assert (await _state(factory, seed))[0] == 4


@pytest.mark.parametrize("key", ["short", "x" * 65, "has spaces in the key!!"])
async def test_a_malformed_key_is_a_422(factory, seed, key):
    app = _app(factory)
    with TestClient(app) as client:
        resp = _post(client, app, seed["user_ids"][0], _body(seed), key)
    assert resp.status_code == 422
    assert (await _state(factory, seed))[0] == 0


async def test_the_purge_deletes_only_expired_keys(factory, seed):
    now = datetime.datetime(2026, 11, 4, 12, 0, 0)
    ages = {"old": KEY_TTL_HOURS * 60 + 1, "fresh": KEY_TTL_HOURS * 60 - 1}
    async with factory() as db:
        for name, minutes in ages.items():
            db.add(TransactionBatchKey(
                org_id=seed["org_id"], user_id=seed["user_ids"][0],
                idempotency_key=f"{name}-key-0123456789", request_hash="0" * 64,
                response={}, created_at=now - datetime.timedelta(minutes=minutes),
            ))
        await db.commit()
    assert await run_batch_key_purge(factory, now=now) == 1
    async with factory() as db:
        left = (await db.execute(select(TransactionBatchKey.idempotency_key))).scalars().all()
    assert left == ["fresh-key-0123456789"]
