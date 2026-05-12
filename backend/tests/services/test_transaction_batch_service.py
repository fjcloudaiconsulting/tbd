"""Tests for the manual batch transaction entry service (L3.2 Wave 2A).

Pins the savepoint isolation invariant: a failing row rolls back its
own savepoint without taking down the rest of the batch. Also covers
the happy path and the not-found / org-scope error surfaces.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.category import CategoryType
from app.models.transaction import Transaction
from app.schemas.import_batch import (
    BatchTransactionRow,
    BatchTransactionsRequest,
)
from app.schemas.transaction import TransactionCreate
from app.services.transaction_batch_service import create_batch


@pytest_asyncio.fixture
async def db_session():
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
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed(db: AsyncSession) -> dict:
    """Seed two orgs (cross-org isolation tests), an account, and
    expense + income categories."""
    org = Organization(name="Primary", billing_cycle_day=1)
    other = Organization(name="Other", billing_cycle_day=1)
    db.add_all([org, other])
    await db.flush()

    at = AccountType(
        org_id=org.id, name="Checking", slug="checking", is_system=True
    )
    db.add(at)
    await db.flush()
    acct = Account(
        org_id=org.id, name="Cash", account_type_id=at.id,
        balance=Decimal("1000.00"), currency="EUR",
    )
    other_at = AccountType(
        org_id=other.id, name="Checking", slug="checking", is_system=True
    )
    db.add(other_at)
    await db.flush()
    other_acct = Account(
        org_id=other.id, name="Other Cash", account_type_id=other_at.id,
        balance=Decimal("0"), currency="EUR",
    )
    db.add_all([acct, other_acct])
    await db.flush()

    expense_cat = Category(
        org_id=org.id, name="Groceries", slug="groceries",
        type=CategoryType.EXPENSE,
    )
    income_cat = Category(
        org_id=org.id, name="Salary", slug="salary",
        type=CategoryType.INCOME,
    )
    db.add_all([expense_cat, income_cat])
    await db.commit()

    return {
        "org_id": org.id,
        "other_org_id": other.id,
        "account_id": acct.id,
        "other_account_id": other_acct.id,
        "expense_cat_id": expense_cat.id,
        "income_cat_id": income_cat.id,
    }


def _row(
    n: int,
    *,
    account_id: int,
    category_id: int,
    amount: str = "10.00",
    description: str | None = None,
    tx_type: str = "expense",
) -> BatchTransactionRow:
    return BatchTransactionRow(
        row_number=n,
        transaction=TransactionCreate(
            account_id=account_id,
            category_id=category_id,
            description=description or f"Row {n}",
            amount=Decimal(amount),
            type=tx_type,
            date=date(2026, 5, 10),
        ),
    )


async def test_create_batch_happy_path_ten_rows(db_session):
    """All ten rows commit, response counters match, savepoint commits
    persist on the outer transaction."""
    seed = await _seed(db_session)
    rows = [
        _row(i, account_id=seed["account_id"], category_id=seed["expense_cat_id"])
        for i in range(1, 11)
    ]
    body = BatchTransactionsRequest(rows=rows)

    response = await create_batch(db_session, seed["org_id"], body)
    await db_session.commit()

    assert response.imported_count == 10
    assert response.error_count == 0
    assert len(response.results) == 10
    assert [r.row_number for r in response.results] == list(range(1, 11))

    # All ten rows are visible on the outer transaction.
    found = (await db_session.execute(
        select(Transaction).where(Transaction.org_id == seed["org_id"])
    )).scalars().all()
    assert len(found) == 10


async def test_create_batch_partial_success_isolates_failing_row(db_session):
    """A row referencing a non-existent category fails; the surrounding
    rows commit. Savepoint rollback must not poison subsequent rows."""
    seed = await _seed(db_session)
    rows = [
        _row(1, account_id=seed["account_id"], category_id=seed["expense_cat_id"]),
        # Row 2: category_id 999_999 doesn't exist → ValidationError.
        _row(2, account_id=seed["account_id"], category_id=999_999),
        _row(3, account_id=seed["account_id"], category_id=seed["expense_cat_id"]),
    ]
    body = BatchTransactionsRequest(rows=rows)

    response = await create_batch(db_session, seed["org_id"], body)
    await db_session.commit()

    assert response.imported_count == 2
    assert response.error_count == 1
    assert {r.row_number for r in response.results} == {1, 3}
    assert response.errors[0].row_number == 2
    assert response.errors[0].error  # human-readable, non-empty

    # Surviving rows committed; bad row left no transaction behind.
    descs = sorted([
        t.description
        for t in (await db_session.execute(
            select(Transaction).where(Transaction.org_id == seed["org_id"])
        )).scalars()
    ])
    assert descs == ["Row 1", "Row 3"]


async def test_create_batch_rejects_cross_org_account(db_session):
    """A row pointing at another org's account fails with a per-row
    error. No leakage across the org boundary."""
    seed = await _seed(db_session)
    rows = [
        _row(
            1,
            account_id=seed["other_account_id"],
            category_id=seed["expense_cat_id"],
        ),
    ]
    body = BatchTransactionsRequest(rows=rows)

    response = await create_batch(db_session, seed["org_id"], body)
    await db_session.commit()

    assert response.imported_count == 0
    assert response.error_count == 1
    assert response.errors[0].row_number == 1


async def test_create_batch_rejects_cross_org_category(db_session):
    """A category that exists in a sibling org is invisible — fails
    org-scope validation."""
    seed = await _seed(db_session)
    # Stash a category in the OTHER org.
    other_cat = Category(
        org_id=seed["other_org_id"],
        name="Their Groceries",
        slug="their-groceries",
        type=CategoryType.EXPENSE,
    )
    db_session.add(other_cat)
    await db_session.commit()

    rows = [
        _row(
            1,
            account_id=seed["account_id"],
            category_id=other_cat.id,
        ),
    ]
    body = BatchTransactionsRequest(rows=rows)

    response = await create_batch(db_session, seed["org_id"], body)
    await db_session.commit()

    assert response.imported_count == 0
    assert response.error_count == 1


async def test_create_batch_rejects_category_type_mismatch(db_session):
    """An income transaction tagged with an expense-only category fails
    at the type guard."""
    seed = await _seed(db_session)
    rows = [
        _row(
            1,
            account_id=seed["account_id"],
            category_id=seed["expense_cat_id"],
            tx_type="income",
        ),
        # Valid expense row alongside — partial success.
        _row(
            2,
            account_id=seed["account_id"],
            category_id=seed["expense_cat_id"],
            tx_type="expense",
        ),
    ]
    body = BatchTransactionsRequest(rows=rows)

    response = await create_batch(db_session, seed["org_id"], body)
    await db_session.commit()

    assert response.imported_count == 1
    assert response.error_count == 1
    assert response.errors[0].row_number == 1
    assert response.results[0].row_number == 2


async def test_create_batch_rows_are_not_imported(db_session):
    """Manual batch rows MUST land with ``is_imported=False`` — they
    are user-typed, not bank-sourced (per spec §0.2)."""
    seed = await _seed(db_session)
    rows = [
        _row(1, account_id=seed["account_id"], category_id=seed["expense_cat_id"]),
    ]
    body = BatchTransactionsRequest(rows=rows)

    response = await create_batch(db_session, seed["org_id"], body)
    await db_session.commit()

    assert response.imported_count == 1
    tx = (await db_session.execute(
        select(Transaction).where(
            Transaction.id == response.results[0].transaction_id
        )
    )).scalar_one()
    assert tx.is_imported is False


async def test_create_batch_updates_account_balance(db_session):
    """Savepoint-committed rows apply balance updates exactly like the
    single-row create path. Three EUR 10 expenses on a EUR 1000 account
    must drop the balance to EUR 970."""
    seed = await _seed(db_session)
    rows = [
        _row(
            i,
            account_id=seed["account_id"],
            category_id=seed["expense_cat_id"],
        )
        for i in range(1, 4)
    ]
    body = BatchTransactionsRequest(rows=rows)

    response = await create_batch(db_session, seed["org_id"], body)
    await db_session.commit()

    assert response.imported_count == 3
    acct = (await db_session.execute(
        select(Account).where(Account.id == seed["account_id"])
    )).scalar_one()
    assert acct.balance == Decimal("970.00")


pytestmark = pytest.mark.asyncio
