"""TBD-784 concurrency fences for the batch-create Idempotency-Key, on REAL MySQL.

Run with BATCH_KEY_MYSQL_URL=mysql+aiomysql://... pointing at a disposable
database already at ``alembic upgrade head``. The fences need the InnoDB
unique-key wait on ``transaction_batch_keys``, which SQLite does not
reproduce. The fixture deletes the rows these tests write.
"""
from __future__ import annotations

import asyncio
import datetime
import os
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.account import Account, AccountType
from app.models.category import Category, CategoryType
from app.models.transaction import Transaction
from app.models.user import Organization, User
from app.schemas.import_batch import BatchTransactionRow, BatchTransactionsRequest
from app.schemas.transaction import TransactionCreate
from app.services import transaction_batch_service

URL = os.environ.get("BATCH_KEY_MYSQL_URL")
if not URL:
    # CI sets REQUIRED=1, so a missing URL there is red, never a skip.
    if os.environ.get("BATCH_KEY_MYSQL_REQUIRED") == "1":
        raise RuntimeError("BATCH_KEY_MYSQL_URL is required here")
    pytestmark = pytest.mark.skipif(True, reason="BATCH_KEY_MYSQL_URL not set")

_CREATED_ORGS: list[int] = []


@pytest_asyncio.fixture
async def factory():
    eng = create_async_engine(URL)
    _CREATED_ORGS.clear()
    try:
        yield async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    finally:
        async with eng.begin() as conn:
            for org_id in _CREATED_ORGS:
                for table in ("transaction_batch_keys", "category_rules", "transactions",
                              "accounts", "categories", "account_types", "users"):
                    await conn.execute(
                        text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": org_id}
                    )
                await conn.execute(
                    text("DELETE FROM organizations WHERE id = :o"), {"o": org_id}
                )
        await eng.dispose()


async def _seed(factory) -> dict:
    async with factory() as db:
        org = Organization(name=f"Batch {uuid.uuid4()}", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        _CREATED_ORGS.append(org.id)
        user = User(username=f"u{uuid.uuid4().hex[:12]}",
                    email=f"{uuid.uuid4().hex[:12]}@example.com",
                    password_hash="x", org_id=org.id, is_superadmin=False)
        atype = AccountType(org_id=org.id, name="Checking", slug="checking",
                            is_system=True)
        db.add_all([user, atype])
        await db.flush()
        account = Account(org_id=org.id, account_type_id=atype.id, name="Checking",
                          balance=Decimal("0"), currency="EUR")
        category = Category(org_id=org.id, name="General", slug="general",
                            is_system=True, type=CategoryType.BOTH)
        db.add_all([account, category])
        await db.commit()
        return {"org_id": org.id, "user_id": user.id, "account_id": account.id,
                "category_id": category.id}


def _body(seed: dict, n: int) -> BatchTransactionsRequest:
    return BatchTransactionsRequest(rows=[
        BatchTransactionRow(row_number=i + 1, transaction=TransactionCreate(
            account_id=seed["account_id"], category_id=seed["category_id"],
            description=f"Row {i + 1}", amount=Decimal("5.00") + i, type="expense",
            date=datetime.date(2026, 11, 1),
        ))
        for i in range(n)
    ])


async def _submit(factory, seed: dict, body, key: str):
    async with factory() as db:
        response = await transaction_batch_service.create_batch_with_key(
            db, seed["org_id"], seed["user_id"], body, key
        )
        await db.commit()
        return response


async def _counts(factory, seed: dict) -> tuple[int, Decimal]:
    async with factory() as db:
        tx = await db.scalar(select(func.count()).select_from(Transaction)
                             .where(Transaction.org_id == seed["org_id"]))
        balance = await db.scalar(select(Account.balance)
                                  .where(Account.id == seed["account_id"]))
    return tx, balance


def _slow_first_batch(monkeypatch, *, fail: bool = False) -> None:
    """Hold the first claim open long enough for the others to hit its index."""
    real = transaction_batch_service.create_batch
    calls = {"n": 0}

    async def slow(db, org_id, body):
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(0.5)
            if fail:
                raise RuntimeError("first request dies after its claim")
        return await real(db, org_id, body)

    monkeypatch.setattr(transaction_batch_service, "create_batch", slow)


async def test_simultaneous_requests_with_one_key_write_once(factory, monkeypatch):
    """FENCE. Wrong implementation: a check-then-insert without the unique
    index wait, which lets every concurrent request write the rows."""
    _slow_first_batch(monkeypatch)
    seed = await _seed(factory)
    body = _body(seed, 10)
    key = uuid.uuid4().hex

    results = await asyncio.gather(*(_submit(factory, seed, body, key) for _ in range(3)))

    assert sorted(r.replayed for r in results) == [False, True, True]
    first = next(r for r in results if not r.replayed)
    for r in results:
        assert r.results == first.results and r.imported_count == 10
    assert await _counts(factory, seed) == (10, -sum(r.transaction.amount for r in body.rows))


async def test_a_waiting_request_writes_when_the_first_rolls_back(factory, monkeypatch):
    """FENCE. Wrong implementation: a claim committed before the rows, which
    leaves the key taken by a request that wrote nothing."""
    _slow_first_batch(monkeypatch, fail=True)
    seed = await _seed(factory)
    body = _body(seed, 5)
    key = uuid.uuid4().hex

    results = await asyncio.gather(
        _submit(factory, seed, body, key), _submit(factory, seed, body, key),
        return_exceptions=True,
    )

    assert sum(isinstance(r, RuntimeError) for r in results) == 1
    (ok,) = [r for r in results if not isinstance(r, BaseException)]
    assert ok.replayed is False and ok.imported_count == 5
    assert (await _counts(factory, seed))[0] == 5
