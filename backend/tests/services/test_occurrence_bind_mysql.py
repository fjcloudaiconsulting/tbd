"""TBD-755 PR2 concurrency fences for binding a bank row to an occurrence, on REAL MySQL.

Run with OCCURRENCE_BIND_MYSQL_URL=mysql+aiomysql://... pointing at a
disposable database already at ``alembic upgrade head``. They need InnoDB row
locks, the UNIQUE ``bound_bank_tx_id`` and REPEATABLE READ, none of which
SQLite reproduces. Each fence races two real connections and then checks the
money: exactly one bank amount per bank row in ``accounts.balance``, and never
two occurrences on one due date. A loser may see InnoDB 1213 (the accepted
residuals); it is retried once, as a client resuming the request would.
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
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.account import Account, AccountType
from app.models.category import Category, CategoryType
from app.models.import_batch import ImportBatch, ImportBatchStatus, ImportSourceFormat
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization, User
from app.schemas.import_reconciliation import (
    ReconcileBatchRequest,
    ReconciliationState,
    ReconciliationTransition,
)
from app.schemas.import_schemas import ImportConfirmRequest, ImportConfirmRow
from app.schemas.recurring import RecurringCreate
from app.services import import_service, reconciliation_service, recurring_service

URL = os.environ.get("OCCURRENCE_BIND_MYSQL_URL")
if not URL:
    # CI sets REQUIRED=1, so a missing URL there is red, never a skip.
    if os.environ.get("OCCURRENCE_BIND_MYSQL_REQUIRED") == "1":
        raise RuntimeError("OCCURRENCE_BIND_MYSQL_URL is required here")
    pytestmark = pytest.mark.skipif(True, reason="OCCURRENCE_BIND_MYSQL_URL not set")

JUN_1 = datetime.date(2026, 6, 1)
JUN_29 = datetime.date(2026, 6, 29)
JUL_1 = datetime.date(2026, 7, 1)
MAY_29 = datetime.date(2026, 5, 29)
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
                await conn.execute(
                    text("UPDATE transactions SET linked_transaction_id = NULL, "
                         "bound_bank_tx_id = NULL WHERE org_id = :o"), {"o": org_id},
                )
                for table in ("transactions", "recurring_transactions", "import_batches",
                              "accounts", "categories", "account_types", "users"):
                    await conn.execute(
                        text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": org_id}
                    )
                await conn.execute(text("DELETE FROM organizations WHERE id = :o"), {"o": org_id})
        await eng.dispose()


async def _seed(factory, *, next_due: datetime.date, generate_today: datetime.date | None) -> dict:
    async with factory() as db:
        org = Organization(name=f"Bind {uuid.uuid4()}", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        _CREATED_ORGS.append(org.id)
        user = User(username=f"u{uuid.uuid4().hex[:12]}", email=f"{uuid.uuid4().hex[:12]}@example.com",
                    password_hash="x", org_id=org.id, is_superadmin=False)
        atype = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
        db.add_all([user, atype])
        await db.flush()
        account = Account(org_id=org.id, account_type_id=atype.id, name="Checking",
                          balance=Decimal("0"), currency="EUR")
        category = Category(org_id=org.id, name="General", slug="general", is_system=True,
                            type=CategoryType.BOTH)
        db.add_all([account, category])
        await db.flush()
        batch = ImportBatch(org_id=org.id, account_id=account.id,
                            source_format=ImportSourceFormat.CSV, file_name="seed.csv",
                            created_by_user_id=user.id, status=ImportBatchStatus.OPEN,
                            row_count=0, accepted_count=0, pending_count=0)
        db.add(batch)
        await db.commit()
        r = await recurring_service.create_recurring(
            db, org.id,
            RecurringCreate(account_id=account.id, category_id=category.id, description="Energy",
                            amount=Decimal("107.00"), type="expense", frequency="monthly",
                            next_due_date=next_due),
            today=JUN_1,
        )
        if generate_today:
            await recurring_service.generate_due_transactions(db, org.id, today=generate_today)
        return {"org_id": org.id, "user_id": user.id, "account_id": account.id,
                "category_id": category.id, "recurring_id": r.id, "batch_id": batch.id}


def _bind_body(seed, *, amount: str, on: datetime.date, due: datetime.date,
               occurrence_id: int | None) -> ImportConfirmRequest:
    return ImportConfirmRequest(
        account_id=seed["account_id"], default_category_id=seed["category_id"],
        file_name="bank.csv", source_format="csv", idempotency_key=uuid.uuid4().hex,
        rows=[ImportConfirmRow(
            row_number=1, date=on, description="Energy", amount=Decimal(amount), type="expense",
            action="bind_occurrence", bind_recurring_id=seed["recurring_id"],
            bind_due_date=due, bind_occurrence_id=occurrence_id,
        )],
    )


async def _retry_once(make):
    try:
        return await make()
    except OperationalError as exc:
        if "1213" not in str(exc) and "1205" not in str(exc):
            raise
        return await make()


async def _confirm(factory, seed, body, today):
    async def go():
        async with factory() as db:
            return await import_service.execute_import(
                db, seed["org_id"], body, user_id=seed["user_id"], today=today,
            )
    return await _retry_once(go)


async def _state(factory, seed):
    async with factory() as db:
        balance = await db.scalar(select(Account.balance).where(Account.id == seed["account_id"]))
        rows = (await db.execute(
            select(Transaction).where(Transaction.org_id == seed["org_id"])
        )).scalars().all()
    return balance, rows


def _counted(rows) -> Decimal:
    """What the balance must be: every SETTLED row not one-way-linked away."""
    total = Decimal("0")
    for t in rows:
        if t.status != TransactionStatus.SETTLED or t.reconciliation_state in ("skipped", "rejected"):
            continue
        if t.linked_transaction_id is not None and t.bind_prev_status is not None:
            continue
        total += t.amount if t.type == TransactionType.INCOME else -t.amount
    return total


async def test_two_confirms_race_for_one_occurrence(factory):
    """The pre-lock and the UNIQUE back-pointer: one binds, the other row is
    imported plain. Never both bound, never a row lost."""
    seed = await _seed(factory, next_due=JUN_1, generate_today=JUN_1)
    async with factory() as db:
        occ_id = await db.scalar(select(Transaction.id).where(
            Transaction.recurring_id == seed["recurring_id"], Transaction.date == JUN_1))

    results = await asyncio.gather(
        _confirm(factory, seed, _bind_body(seed, amount="365.00", on=MAY_29, due=JUN_1,
                                           occurrence_id=occ_id), JUN_1),
        _confirm(factory, seed, _bind_body(seed, amount="300.00", on=MAY_29, due=JUN_1,
                                           occurrence_id=occ_id), JUN_1),
    )

    assert sorted(r.bound_count for r in results) == [0, 1]
    assert sorted(len(r.bind_fallbacks) for r in results) == [0, 1]
    balance, rows = await _state(factory, seed)
    assert balance == _counted(rows) == Decimal("-665.00")
    assert sum(1 for t in rows if t.bound_bank_tx_id is not None) == 1


async def test_create_path_bind_races_generation(factory):
    """Template lock: generation and the bind never both write 1 July."""
    seed = await _seed(factory, next_due=JUL_1, generate_today=None)

    async def generate():
        async def go():
            async with factory() as db:
                await recurring_service.generate_due_transactions(db, seed["org_id"], today=JUL_1)
        await _retry_once(go)

    await asyncio.gather(
        _confirm(factory, seed, _bind_body(seed, amount="365.00", on=JUN_29, due=JUL_1,
                                           occurrence_id=None), JUN_29),
        generate(),
    )

    balance, rows = await _state(factory, seed)
    series = [t for t in rows if t.recurring_id == seed["recurring_id"]]
    assert len(series) == 1
    assert balance == _counted(rows)


async def test_bind_races_a_reconcile_match(factory):
    """Occurrence row lock first on both paths: exactly one bank row settles it."""
    seed = await _seed(factory, next_due=JUN_1, generate_today=JUN_1)
    async with factory() as db:
        occ_id = await db.scalar(select(Transaction.id).where(
            Transaction.recurring_id == seed["recurring_id"], Transaction.date == JUN_1))
        other = Transaction(org_id=seed["org_id"], account_id=seed["account_id"],
                            category_id=seed["category_id"], description="Energy",
                            amount=Decimal("300.00"), type=TransactionType.EXPENSE,
                            status=TransactionStatus.SETTLED, date=MAY_29, settled_date=MAY_29,
                            is_imported=True, import_batch_id=seed["batch_id"],
                            reconciliation_state="pending_review")
        db.add(other)
        acct = await db.get(Account, seed["account_id"])
        acct.balance -= Decimal("300.00")
        batch = await db.get(ImportBatch, seed["batch_id"])
        batch.row_count, batch.pending_count = 1, 1
        await db.commit()
        other_id = other.id

    async def match():
        async def go():
            async with factory() as db:
                await reconciliation_service.reconcile_request(
                    db, org_id=seed["org_id"], batch_id=seed["batch_id"],
                    request=ReconcileBatchRequest(transitions=[ReconciliationTransition(
                        transaction_id=other_id, to_state=ReconciliationState.MATCHED,
                        match_with_transaction_id=occ_id,
                    )]),
                )
        try:
            await _retry_once(go)
        except Exception:
            pass  # the loser's 409 is the expected outcome

    await asyncio.gather(
        _confirm(factory, seed, _bind_body(seed, amount="365.00", on=MAY_29, due=JUN_1,
                                           occurrence_id=occ_id), JUN_1),
        match(),
    )

    balance, rows = await _state(factory, seed)
    occ = next(t for t in rows if t.id == occ_id)
    assert occ.bound_bank_tx_id is not None
    assert sum(1 for t in rows if t.bind_prev_status is not None) == 1
    assert balance == _counted(rows) == Decimal("-665.00")
