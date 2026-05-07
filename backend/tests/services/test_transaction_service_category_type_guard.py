"""Server-side guard: reject transactions whose resolved category type
disagrees with the transaction's type. Closes the HIGH finding from PR #137
review (transaction writes don't validate category/type compatibility).

The guard fires on every write entrypoint that takes a user-supplied
(type, category_id) pair: create_transaction, _create_transaction_no_commit
(used by import_service), and update_transaction (when either field changes).

Transfer legs use the system Transfer category (CategoryType.BOTH) so the
pairing flow stays compatible with the new guard. A regression test pins
that create_transfer still works.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.category import CategoryType
from app.models.transaction import TransactionType
from app.schemas.transaction import (
    TransactionCreate,
    TransactionUpdate,
    TransferCreate,
)
from app.services import transaction_service
from app.services.exceptions import ValidationError


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
    """Org + accounts + one EXPENSE-only master, one INCOME-only master,
    one BOTH master, plus an EXPENSE subcategory under the EXPENSE master.
    """
    org = Organization(name="Test", billing_cycle_day=1)
    db.add(org)
    await db.flush()
    at = AccountType(
        org_id=org.id, name="Checking", slug="checking", is_system=True
    )
    db.add(at)
    await db.flush()
    src = Account(
        org_id=org.id, name="Src", account_type_id=at.id,
        balance=Decimal("1000"), currency="EUR",
    )
    dst = Account(
        org_id=org.id, name="Dst", account_type_id=at.id,
        balance=Decimal("0"), currency="EUR",
    )
    db.add_all([src, dst])
    await db.flush()

    expense_master = Category(
        org_id=org.id, name="Groceries", slug="groceries",
        type=CategoryType.EXPENSE,
    )
    income_master = Category(
        org_id=org.id, name="Salary", slug="salary",
        type=CategoryType.INCOME,
    )
    both_master = Category(
        org_id=org.id, name="Transfer", slug="transfer",
        type=CategoryType.BOTH, is_system=True,
    )
    db.add_all([expense_master, income_master, both_master])
    await db.flush()

    expense_sub = Category(
        org_id=org.id, parent_id=expense_master.id, name="Supermarket",
        slug="supermarket", type=CategoryType.EXPENSE,
    )
    db.add(expense_sub)
    await db.commit()

    return {
        "org_id": org.id,
        "src_id": src.id,
        "dst_id": dst.id,
        "expense_master_id": expense_master.id,
        "income_master_id": income_master.id,
        "both_master_id": both_master.id,
        "expense_sub_id": expense_sub.id,
    }


# ── create_transaction ─────────────────────────────────────────────────────


async def test_create_rejects_income_with_expense_category(db_session):
    """An income transaction tagged with an expense-only category is
    rejected. The mismatch must surface through ValidationError so the
    router maps it to a 400 (the codebase's standard validation surface)."""
    seed = await _seed(db_session)
    body = TransactionCreate(
        account_id=seed["src_id"],
        category_id=seed["expense_master_id"],
        description="paycheck",
        amount=Decimal("100"),
        type="income",
        status="settled",
        date=date(2026, 5, 1),
    )
    with pytest.raises(ValidationError):
        await transaction_service.create_transaction(
            db_session, seed["org_id"], body
        )


async def test_create_rejects_expense_with_income_category(db_session):
    """Mirror of the above: an expense tagged with an income-only
    category is rejected."""
    seed = await _seed(db_session)
    body = TransactionCreate(
        account_id=seed["src_id"],
        category_id=seed["income_master_id"],
        description="rent",
        amount=Decimal("500"),
        type="expense",
        status="settled",
        date=date(2026, 5, 1),
    )
    with pytest.raises(ValidationError):
        await transaction_service.create_transaction(
            db_session, seed["org_id"], body
        )


async def test_create_accepts_matching_category(db_session):
    """Sanity: matching type/category passes the guard."""
    seed = await _seed(db_session)
    body = TransactionCreate(
        account_id=seed["src_id"],
        category_id=seed["expense_master_id"],
        description="grocery run",
        amount=Decimal("25"),
        type="expense",
        status="settled",
        date=date(2026, 5, 1),
    )
    tx = await transaction_service.create_transaction(
        db_session, seed["org_id"], body
    )
    assert tx.id is not None


async def test_create_accepts_both_category_for_either_type(db_session):
    """CategoryType.BOTH (e.g. the system Transfer category) is allowed
    on both income and expense transactions."""
    seed = await _seed(db_session)
    expense_body = TransactionCreate(
        account_id=seed["src_id"],
        category_id=seed["both_master_id"],
        description="x",
        amount=Decimal("1"),
        type="expense",
        status="settled",
        date=date(2026, 5, 1),
    )
    income_body = TransactionCreate(
        account_id=seed["dst_id"],
        category_id=seed["both_master_id"],
        description="y",
        amount=Decimal("1"),
        type="income",
        status="settled",
        date=date(2026, 5, 1),
    )
    await transaction_service.create_transaction(
        db_session, seed["org_id"], expense_body
    )
    await transaction_service.create_transaction(
        db_session, seed["org_id"], income_body
    )


async def test_create_rejects_subcategory_when_parent_master_mismatches(db_session):
    """A subcategory under an expense master can't be used for an income
    transaction even if the subcategory itself is CategoryType.EXPENSE
    (which it always is in this seed). The guard treats any expense-rooted
    chain as expense-only against income transactions."""
    seed = await _seed(db_session)
    body = TransactionCreate(
        account_id=seed["src_id"],
        category_id=seed["expense_sub_id"],
        description="paycheck via subcategory",
        amount=Decimal("100"),
        type="income",
        status="settled",
        date=date(2026, 5, 1),
    )
    with pytest.raises(ValidationError):
        await transaction_service.create_transaction(
            db_session, seed["org_id"], body
        )


# ── update_transaction ─────────────────────────────────────────────────────


async def test_update_rejects_swapping_in_mismatched_category(db_session):
    """Update path: existing expense transaction can't have its category
    swapped to an income-only one."""
    seed = await _seed(db_session)
    create_body = TransactionCreate(
        account_id=seed["src_id"],
        category_id=seed["expense_master_id"],
        description="initial",
        amount=Decimal("10"),
        type="expense",
        status="settled",
        date=date(2026, 5, 1),
    )
    tx = await transaction_service.create_transaction(
        db_session, seed["org_id"], create_body
    )

    update_body = TransactionUpdate(category_id=seed["income_master_id"])
    with pytest.raises(ValidationError):
        await transaction_service.update_transaction(
            db_session, seed["org_id"], tx.id, update_body
        )


async def test_update_rejects_flipping_type_against_existing_category(db_session):
    """Update path: flipping type from expense to income while the row
    still points at an expense-only category is rejected."""
    seed = await _seed(db_session)
    create_body = TransactionCreate(
        account_id=seed["src_id"],
        category_id=seed["expense_master_id"],
        description="initial",
        amount=Decimal("10"),
        type="expense",
        status="settled",
        date=date(2026, 5, 1),
    )
    tx = await transaction_service.create_transaction(
        db_session, seed["org_id"], create_body
    )

    update_body = TransactionUpdate(type="income")
    with pytest.raises(ValidationError):
        await transaction_service.update_transaction(
            db_session, seed["org_id"], tx.id, update_body
        )


async def test_update_accepts_matching_category_swap(db_session):
    """Swapping category to a compatible one (or to BOTH) succeeds."""
    seed = await _seed(db_session)
    create_body = TransactionCreate(
        account_id=seed["src_id"],
        category_id=seed["expense_master_id"],
        description="initial",
        amount=Decimal("10"),
        type="expense",
        status="settled",
        date=date(2026, 5, 1),
    )
    tx = await transaction_service.create_transaction(
        db_session, seed["org_id"], create_body
    )

    update_body = TransactionUpdate(category_id=seed["both_master_id"])
    updated = await transaction_service.update_transaction(
        db_session, seed["org_id"], tx.id, update_body
    )
    assert updated.category_id == seed["both_master_id"]


async def test_update_accepts_simultaneous_type_and_category_swap(db_session):
    """If both type and category change in one call, the new pair must
    be compatible — and is, in the happy path."""
    seed = await _seed(db_session)
    create_body = TransactionCreate(
        account_id=seed["src_id"],
        category_id=seed["expense_master_id"],
        description="initial",
        amount=Decimal("10"),
        type="expense",
        status="settled",
        date=date(2026, 5, 1),
    )
    tx = await transaction_service.create_transaction(
        db_session, seed["org_id"], create_body
    )

    update_body = TransactionUpdate(
        type="income", category_id=seed["income_master_id"]
    )
    updated = await transaction_service.update_transaction(
        db_session, seed["org_id"], tx.id, update_body
    )
    assert updated.type == TransactionType.INCOME
    assert updated.category_id == seed["income_master_id"]


# ── _create_transaction_no_commit (import path) ────────────────────────────


async def test_create_no_commit_rejects_mismatched_category(db_session):
    """The internal primitive used by import_service must enforce the
    same guard, so /api/v1/import/execute can't smuggle mismatched rows."""
    seed = await _seed(db_session)
    body = TransactionCreate(
        account_id=seed["src_id"],
        category_id=seed["income_master_id"],
        description="bogus",
        amount=Decimal("5"),
        type="expense",
        status="settled",
        date=date(2026, 5, 1),
    )
    with pytest.raises(ValidationError):
        async with db_session.begin_nested():
            await transaction_service._create_transaction_no_commit(
                db_session, seed["org_id"], body, is_imported=True
            )


# ── transfer regression ────────────────────────────────────────────────────


async def test_create_transfer_still_works_with_both_category(db_session):
    """Regression: transfers use the system Transfer category (CategoryType.BOTH).
    The new guard must not break the existing transfer flow."""
    seed = await _seed(db_session)
    body = TransferCreate(
        from_account_id=seed["src_id"],
        to_account_id=seed["dst_id"],
        category_id=seed["both_master_id"],
        amount=Decimal("100"),
        date=date(2026, 5, 1),
        status="settled",
    )
    expense_tx, income_tx = await transaction_service.create_transfer(
        db_session, seed["org_id"], body
    )
    assert expense_tx.category_id == seed["both_master_id"]
    assert income_tx.category_id == seed["both_master_id"]
    assert expense_tx.linked_transaction_id == income_tx.id
