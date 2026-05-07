"""Server-side guard: reject transfer flows that try to assign a one-sided
category (INCOME-only or EXPENSE-only) to a transfer pair.

Closes the HIGH finding from PR #150 round 2: ``create_transfer`` and
``_link_pair`` (via ``pair_existing_transactions`` / ``convert_and_create_leg``)
previously called ``validate_category`` (existence only) on a user-supplied
``transfer_category_id`` and then assigned the same category to BOTH legs.
With a one-sided category that meant the income leg ended up with an
expense-only category (or vice versa), bypassing the new (type, category)
guard added on the regular write paths.

Rule: any user-supplied transfer category MUST be ``CategoryType.BOTH``.
Both legs share one category by design, so a one-sided category is
structurally wrong on a transfer regardless of which leg you look at.
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

from app.models import Account, AccountType, Category, Organization, Transaction
from app.models.base import Base
from app.models.category import CategoryType
from app.models.transaction import TransactionStatus, TransactionType
from app.schemas.transaction import TransferCreate
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
    org = Organization(name="T", billing_cycle_day=1)
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

    transfer_cat = Category(
        org_id=org.id, name="Transfer", slug="transfer",
        type=CategoryType.BOTH, is_system=True,
    )
    expense_only = Category(
        org_id=org.id, name="Groceries", slug="groceries",
        type=CategoryType.EXPENSE,
    )
    income_only = Category(
        org_id=org.id, name="Salary", slug="salary",
        type=CategoryType.INCOME,
    )
    other_both = Category(
        org_id=org.id, name="Internal", slug="internal",
        type=CategoryType.BOTH,
    )
    db.add_all([transfer_cat, expense_only, income_only, other_both])
    await db.commit()

    return {
        "org_id": org.id,
        "src_id": src.id,
        "dst_id": dst.id,
        "transfer_cat_id": transfer_cat.id,
        "expense_only_id": expense_only.id,
        "income_only_id": income_only.id,
        "other_both_id": other_both.id,
    }


# ── create_transfer ────────────────────────────────────────────────────────


async def test_create_transfer_rejects_expense_only_category(db_session):
    """User-supplied expense-only category for a transfer is rejected.
    Otherwise the income leg ends up with an expense-only category."""
    seed = await _seed(db_session)
    body = TransferCreate(
        from_account_id=seed["src_id"],
        to_account_id=seed["dst_id"],
        category_id=seed["expense_only_id"],
        amount=Decimal("50"),
        date=date(2026, 5, 1),
        status="settled",
    )
    with pytest.raises(ValidationError):
        await transaction_service.create_transfer(
            db_session, seed["org_id"], body
        )


async def test_create_transfer_rejects_income_only_category(db_session):
    """User-supplied income-only category for a transfer is rejected.
    Otherwise the expense leg ends up with an income-only category."""
    seed = await _seed(db_session)
    body = TransferCreate(
        from_account_id=seed["src_id"],
        to_account_id=seed["dst_id"],
        category_id=seed["income_only_id"],
        amount=Decimal("50"),
        date=date(2026, 5, 1),
        status="settled",
    )
    with pytest.raises(ValidationError):
        await transaction_service.create_transfer(
            db_session, seed["org_id"], body
        )


async def test_create_transfer_accepts_default_transfer_category(db_session):
    """Default path: no category_id supplied, system Transfer category
    (CategoryType.BOTH) is auto-assigned. Sanity check that the new guard
    doesn't break the happy path."""
    seed = await _seed(db_session)
    body = TransferCreate(
        from_account_id=seed["src_id"],
        to_account_id=seed["dst_id"],
        amount=Decimal("50"),
        date=date(2026, 5, 1),
        status="settled",
    )
    expense_tx, income_tx = await transaction_service.create_transfer(
        db_session, seed["org_id"], body
    )
    assert expense_tx.category_id == seed["transfer_cat_id"]
    assert income_tx.category_id == seed["transfer_cat_id"]


async def test_create_transfer_accepts_custom_both_category(db_session):
    """User can override the default with another CategoryType.BOTH category."""
    seed = await _seed(db_session)
    body = TransferCreate(
        from_account_id=seed["src_id"],
        to_account_id=seed["dst_id"],
        category_id=seed["other_both_id"],
        amount=Decimal("50"),
        date=date(2026, 5, 1),
        status="settled",
    )
    expense_tx, income_tx = await transaction_service.create_transfer(
        db_session, seed["org_id"], body
    )
    assert expense_tx.category_id == seed["other_both_id"]
    assert income_tx.category_id == seed["other_both_id"]


# ── pair_existing_transactions / _link_pair ────────────────────────────────


async def _seed_pair_rows(db: AsyncSession, seed: dict) -> tuple[int, int]:
    """Two un-linked rows (one expense on src, one income on dst) ready to
    be paired by pair_existing_transactions."""
    expense = Transaction(
        org_id=seed["org_id"], account_id=seed["src_id"],
        category_id=seed["transfer_cat_id"],
        description="x", amount=Decimal("50"),
        type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
        date=date(2026, 5, 1), settled_date=date(2026, 5, 1),
    )
    income = Transaction(
        org_id=seed["org_id"], account_id=seed["dst_id"],
        category_id=seed["transfer_cat_id"],
        description="x", amount=Decimal("50"),
        type=TransactionType.INCOME, status=TransactionStatus.SETTLED,
        date=date(2026, 5, 1), settled_date=date(2026, 5, 1),
    )
    db.add_all([expense, income])
    await db.commit()
    return expense.id, income.id


async def test_pair_existing_rejects_expense_only_category(db_session):
    """pair_existing_transactions with a user-supplied expense-only
    transfer_category_id is rejected before either leg's category_id mutates.
    """
    seed = await _seed(db_session)
    expense_id, income_id = await _seed_pair_rows(db_session, seed)
    with pytest.raises(ValidationError):
        await transaction_service.pair_existing_transactions(
            db_session, seed["org_id"],
            expense_tx_id=expense_id,
            income_tx_id=income_id,
            recategorize=True,
            transfer_category_id=seed["expense_only_id"],
        )


async def test_pair_existing_rejects_income_only_category(db_session):
    """Same rejection for income-only categories."""
    seed = await _seed(db_session)
    expense_id, income_id = await _seed_pair_rows(db_session, seed)
    with pytest.raises(ValidationError):
        await transaction_service.pair_existing_transactions(
            db_session, seed["org_id"],
            expense_tx_id=expense_id,
            income_tx_id=income_id,
            recategorize=True,
            transfer_category_id=seed["income_only_id"],
        )


async def test_pair_existing_accepts_both_category(db_session):
    """Custom BOTH category is allowed."""
    seed = await _seed(db_session)
    expense_id, income_id = await _seed_pair_rows(db_session, seed)
    e_tx, i_tx = await transaction_service.pair_existing_transactions(
        db_session, seed["org_id"],
        expense_tx_id=expense_id,
        income_tx_id=income_id,
        recategorize=True,
        transfer_category_id=seed["other_both_id"],
    )
    assert e_tx.category_id == seed["other_both_id"]
    assert i_tx.category_id == seed["other_both_id"]
