"""TBD-737 concurrency fences for the import confirm, on REAL MySQL.

Run with IMPORT_CONFIRM_MYSQL_URL=mysql+aiomysql://... pointing at a
disposable database already at ``alembic upgrade head``. The fences need
InnoDB row locks and the unique index on ``import_batches``, which SQLite
does not reproduce. The fixture deletes the rows these tests write.
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
from app.models.import_batch import ImportBatch
from app.models.transaction import Transaction
from app.models.user import Organization, User
from app.schemas.import_schemas import ImportConfirmRequest, ImportConfirmRow
from app.services import import_service

URL = os.environ.get("IMPORT_CONFIRM_MYSQL_URL")
if not URL:
    # CI sets REQUIRED=1, so a missing URL there is red, never a skip.
    if os.environ.get("IMPORT_CONFIRM_MYSQL_REQUIRED") == "1":
        raise RuntimeError("IMPORT_CONFIRM_MYSQL_URL is required here")
    pytestmark = pytest.mark.skipif(True, reason="IMPORT_CONFIRM_MYSQL_URL not set")

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
                for table in ("transactions", "import_batches", "accounts",
                              "categories", "account_types", "users"):
                    await conn.execute(
                        text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": org_id}
                    )
                await conn.execute(
                    text("DELETE FROM organizations WHERE id = :o"), {"o": org_id}
                )
        await eng.dispose()


async def _seed(factory) -> dict:
    async with factory() as db:
        org = Organization(name=f"Confirm {uuid.uuid4()}", billing_cycle_day=1)
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


def _body(seed: dict, n: int, key: str) -> ImportConfirmRequest:
    return ImportConfirmRequest(
        account_id=seed["account_id"],
        default_category_id=seed["category_id"],
        rows=[
            ImportConfirmRow(
                row_number=i + 1,
                date=datetime.date(2026, 5, 1) + datetime.timedelta(days=i % 20),
                description=f"Row {i + 1}",
                amount=Decimal("5.00") + i,
                type="expense" if i % 3 else "income",
            )
            for i in range(n)
        ],
        file_name="bank.csv",
        source_format="csv",
        idempotency_key=key,
    )


async def _confirm(factory, seed: dict, body: ImportConfirmRequest):
    async with factory() as db:
        return await import_service.execute_import(
            db, seed["org_id"], body, user_id=seed["user_id"]
        )


async def test_concurrent_confirms_with_one_key_import_each_row_once(
    factory, monkeypatch,
):
    monkeypatch.setattr(import_service, "CONFIRM_CHUNK_ROWS", 4)
    seed = await _seed(factory)
    body = _body(seed, 30, key=uuid.uuid4().hex)
    expected_balance = sum(
        (r.amount if r.type == "income" else -r.amount) for r in body.rows
    )

    results = await asyncio.gather(*(_confirm(factory, seed, body) for _ in range(3)))

    assert sorted(r.replayed for r in results) == [False, True, True]
    for r in results:
        assert r.imported_count == 30 and r.error_count == 0
        assert r.import_id == results[0].import_id is not None
    async with factory() as db:
        tx = await db.scalar(
            select(func.count()).select_from(Transaction)
            .where(Transaction.org_id == seed["org_id"])
        )
        linked = await db.scalar(
            select(func.count()).select_from(Transaction)
            .where(Transaction.import_batch_id == results[0].import_id)
        )
        balance = await db.scalar(
            select(Account.balance).where(Account.id == seed["account_id"])
        )
        batches = (await db.execute(
            select(ImportBatch).where(ImportBatch.org_id == seed["org_id"])
        )).scalars().all()
    assert tx == linked == 30
    assert balance == expected_balance
    assert len(batches) == 1 and batches[0].row_count == 30


async def test_different_keys_are_independent_imports(factory, monkeypatch):
    monkeypatch.setattr(import_service, "CONFIRM_CHUNK_ROWS", 4)
    seed = await _seed(factory)
    first, second = await asyncio.gather(
        _confirm(factory, seed, _body(seed, 6, key=uuid.uuid4().hex)),
        _confirm(factory, seed, _body(seed, 6, key=uuid.uuid4().hex)),
    )
    assert not first.replayed and not second.replayed
    assert first.import_id != second.import_id
    async with factory() as db:
        tx = await db.scalar(
            select(func.count()).select_from(Transaction)
            .where(Transaction.org_id == seed["org_id"])
        )
    assert tx == 12
