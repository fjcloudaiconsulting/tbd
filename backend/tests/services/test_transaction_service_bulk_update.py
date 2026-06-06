"""bulk_update_transactions service tests (PR 3 — batch edit).

Covers the contract for the batch-edit backend:

- Category/status/account applied to regular rows via update_transaction.
- Transfer legs accept category only (cascading to the partner); status,
  account, and tags requested in the same call are skipped (not applied to
  the transfer leg).
- A non-BOTH category on a transfer leg is rejected by the guard, so the row
  is skipped with a reason.
- A transfer leg whose only requested field is status/account/tags applies
  nothing and is reported in skipped.
- Tags MERGE (union with existing) rather than replace.
- Manual balance adjustments and unknown ids are reported as skipped, not
  fatal (partial success).
- Duplicate ids are deduped.

Runs on SQLite in-memory; no MySQL.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload
from sqlalchemy.pool import StaticPool
from sqlalchemy import select

from app.models import Account, AccountType, Category, Organization, Transaction
from app.models.base import Base
from app.models.category import CategoryType
from app.models.transaction import TransactionStatus, TransactionType
from app.models.user import Role, User
from app.schemas.transaction import TransferCreate
from app.security import hash_password
from app.services import tag_service, transaction_service


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

    user = User(
        org_id=org.id,
        username="u-t",
        email="u@t.example",
        password_hash=hash_password("pw-1234567"),
        role=Role.OWNER,
        is_active=True,
        email_verified=True,
    )
    db.add(user)
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
    acct3 = Account(
        org_id=org.id, name="Third", account_type_id=at.id,
        balance=Decimal("0"), currency="EUR",
    )
    db.add_all([src, dst, acct3])
    await db.flush()

    transfer_cat = Category(
        org_id=org.id, name="Transfer", slug="transfer",
        type=CategoryType.BOTH, is_system=True,
    )
    expense_only = Category(
        org_id=org.id, name="Groceries", slug="groceries",
        type=CategoryType.EXPENSE,
    )
    other_expense = Category(
        org_id=org.id, name="Dining", slug="dining",
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
    db.add_all([transfer_cat, expense_only, other_expense, income_only, other_both])
    await db.commit()

    return {
        "org_id": org.id,
        "user_id": user.id,
        "src_id": src.id,
        "dst_id": dst.id,
        "acct3_id": acct3.id,
        "transfer_cat_id": transfer_cat.id,
        "expense_only_id": expense_only.id,
        "other_expense_id": other_expense.id,
        "income_only_id": income_only.id,
        "other_both_id": other_both.id,
    }


async def _make_transfer(db: AsyncSession, seed: dict) -> tuple[int, int]:
    """Create a real linked transfer pair via the service. Returns
    (expense_leg_id, income_leg_id)."""
    body = TransferCreate(
        from_account_id=seed["src_id"],
        to_account_id=seed["dst_id"],
        category_id=seed["transfer_cat_id"],
        amount=Decimal("50"),
        date=date(2026, 5, 1),
        status="settled",
    )
    expense_tx, income_tx = await transaction_service.create_transfer(
        db, seed["org_id"], body
    )
    return expense_tx.id, income_tx.id


async def _make_expense(db: AsyncSession, seed: dict, *, category_id: int) -> int:
    """Insert a settled expense row on the src account and commit. Returns id."""
    tx = Transaction(
        org_id=seed["org_id"], account_id=seed["src_id"],
        category_id=category_id,
        description="lunch", amount=Decimal("10"),
        type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
        date=date(2026, 5, 1), settled_date=date(2026, 5, 1),
    )
    db.add(tx)
    await db.commit()
    return tx.id


async def _make_manual_adjustment(db: AsyncSession, seed: dict) -> int:
    """Insert a manual balance adjustment row and commit. Returns id."""
    tx = Transaction(
        org_id=seed["org_id"], account_id=seed["src_id"],
        category_id=seed["expense_only_id"],
        description="adj", amount=Decimal("5"),
        type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
        date=date(2026, 5, 1), settled_date=date(2026, 5, 1),
        is_manual_adjustment=True,
    )
    db.add(tx)
    await db.commit()
    return tx.id


async def test_bulk_update_sets_category_on_regular_rows(db_session):
    """Category applied to multiple regular rows; updated_count correct."""
    seed = await _seed(db_session)
    a = await _make_expense(db_session, seed, category_id=seed["expense_only_id"])
    b = await _make_expense(db_session, seed, category_id=seed["expense_only_id"])
    updated, skipped = await transaction_service.bulk_update_transactions(
        db_session, seed["org_id"], [a, b],
        category_id=seed["other_expense_id"], actor_user_id=seed["user_id"],
    )
    assert updated == 2
    assert skipped == []
    assert (await db_session.get(Transaction, a)).category_id == seed["other_expense_id"]


async def test_bulk_update_transfer_category_cascades_and_skips_other_fields(db_session):
    """A transfer leg: category (BOTH) applies + cascades to partner; status and
    account requested in the same call are skipped (not applied to the transfer)."""
    seed = await _seed(db_session)
    exp_id, inc_id = await _make_transfer(db_session, seed)  # both on transfer_cat
    updated, skipped = await transaction_service.bulk_update_transactions(
        db_session, seed["org_id"], [exp_id],
        category_id=seed["other_both_id"], status="pending",
        account_id=seed["acct3_id"], actor_user_id=seed["user_id"],
    )
    assert updated == 1  # category applied
    # partner cascaded
    assert (await db_session.get(Transaction, exp_id)).category_id == seed["other_both_id"]
    assert (await db_session.get(Transaction, inc_id)).category_id == seed["other_both_id"]
    # status/account NOT changed on the transfer leg
    assert (await db_session.get(Transaction, exp_id)).status == TransactionStatus.SETTLED


async def test_bulk_update_transfer_nonboth_category_skips_row(db_session):
    """A non-BOTH category on a transfer leg is rejected by the guard → the row
    is skipped with a reason (no other field requested)."""
    seed = await _seed(db_session)
    exp_id, _ = await _make_transfer(db_session, seed)
    updated, skipped = await transaction_service.bulk_update_transactions(
        db_session, seed["org_id"], [exp_id],
        category_id=seed["expense_only_id"], actor_user_id=seed["user_id"],
    )
    assert updated == 0
    assert len(skipped) == 1 and skipped[0][0] == exp_id


async def test_bulk_update_transfer_only_account_requested_is_skipped(db_session):
    """Transfer leg with ONLY account requested (no category): nothing applies →
    row reported as skipped."""
    seed = await _seed(db_session)
    exp_id, _ = await _make_transfer(db_session, seed)
    updated, skipped = await transaction_service.bulk_update_transactions(
        db_session, seed["org_id"], [exp_id],
        account_id=seed["acct3_id"], actor_user_id=seed["user_id"],
    )
    assert updated == 0
    assert len(skipped) == 1 and skipped[0][0] == exp_id


async def test_bulk_update_status_and_account_on_regular_row(db_session):
    """Regular row: status + account both apply via update_transaction (balances
    handled there)."""
    seed = await _seed(db_session)
    a = await _make_expense(db_session, seed, category_id=seed["expense_only_id"])
    updated, skipped = await transaction_service.bulk_update_transactions(
        db_session, seed["org_id"], [a],
        status="pending", account_id=seed["acct3_id"], actor_user_id=seed["user_id"],
    )
    assert updated == 1 and skipped == []
    row = await db_session.get(Transaction, a)
    assert row.status == TransactionStatus.PENDING
    assert row.account_id == seed["acct3_id"]


async def test_bulk_update_merges_tags_on_regular_row(db_session):
    """Tags MERGE (union) with existing, not replace."""
    seed = await _seed(db_session)
    a = await _make_expense(db_session, seed, category_id=seed["expense_only_id"])
    await tag_service.set_transaction_tags(
        db_session, org_id=seed["org_id"], transaction_id=a,
        tag_names=["existing"], created_by_user_id=seed["user_id"],
    )
    await db_session.commit()
    updated, skipped = await transaction_service.bulk_update_transactions(
        db_session, seed["org_id"], [a], tags=["added"], actor_user_id=seed["user_id"],
    )
    assert updated == 1 and skipped == []
    row = await db_session.scalar(
        select(Transaction).options(selectinload(Transaction.tags)).where(Transaction.id == a)
    )
    names = sorted(t.name_normalized for t in row.tags)
    assert names == ["added", "existing"]


async def test_bulk_update_transfer_tags_skipped(db_session):
    """Tags are not applied to transfer legs."""
    seed = await _seed(db_session)
    exp_id, _ = await _make_transfer(db_session, seed)
    updated, skipped = await transaction_service.bulk_update_transactions(
        db_session, seed["org_id"], [exp_id],
        category_id=seed["other_both_id"], tags=["x"], actor_user_id=seed["user_id"],
    )
    assert updated == 1  # category applied; tags ignored for transfer
    row = await db_session.scalar(
        select(Transaction).options(selectinload(Transaction.tags)).where(Transaction.id == exp_id)
    )
    assert row.tags == []


async def test_bulk_update_skips_manual_adjustment_and_missing(db_session):
    """Manual adjustments and unknown ids are reported as skipped, not fatal."""
    seed = await _seed(db_session)
    a = await _make_expense(db_session, seed, category_id=seed["expense_only_id"])
    adj = await _make_manual_adjustment(db_session, seed)
    updated, skipped = await transaction_service.bulk_update_transactions(
        db_session, seed["org_id"], [a, adj, 999999],
        category_id=seed["other_expense_id"], actor_user_id=seed["user_id"],
    )
    assert updated == 1
    skipped_ids = {s[0] for s in skipped}
    assert skipped_ids == {adj, 999999}


async def test_bulk_update_dedupes_ids(db_session):
    seed = await _seed(db_session)
    a = await _make_expense(db_session, seed, category_id=seed["expense_only_id"])
    updated, skipped = await transaction_service.bulk_update_transactions(
        db_session, seed["org_id"], [a, a],
        category_id=seed["other_expense_id"], actor_user_id=seed["user_id"],
    )
    assert updated == 1 and skipped == []


async def test_bulk_update_ignores_cross_org_ids(db_session):
    """A row in this org is never touched when the call is scoped to another
    org: the id is reported skipped (not found) and the row stays unchanged."""
    seed = await _seed(db_session)
    a = await _make_expense(db_session, seed, category_id=seed["expense_only_id"])
    other_org = Organization(name="Other", billing_cycle_day=1)
    db_session.add(other_org)
    await db_session.commit()

    updated, skipped = await transaction_service.bulk_update_transactions(
        db_session, other_org.id, [a],
        category_id=seed["other_expense_id"], actor_user_id=seed["user_id"],
    )
    assert updated == 0
    assert {s[0] for s in skipped} == {a}
    # Row untouched: still on its original category.
    assert (await db_session.get(Transaction, a)).category_id == seed["expense_only_id"]


async def test_bulk_update_counts_row_updated_when_fields_apply_but_tags_fail(db_session):
    """The trickiest branch: category applies, but the tag merge exceeds the
    per-transaction cap and fails. The row still counts as updated (the
    committed category change is preserved), is NOT in skipped, and its tag set
    is rolled back to the original."""
    seed = await _seed(db_session)
    a = await _make_expense(db_session, seed, category_id=seed["expense_only_id"])
    # Fill the row to the 5-tag cap so adding one more overflows on merge.
    full = ["t1", "t2", "t3", "t4", "t5"]
    await tag_service.set_transaction_tags(
        db_session, org_id=seed["org_id"], transaction_id=a,
        tag_names=full, created_by_user_id=seed["user_id"],
    )
    await db_session.commit()

    updated, skipped = await transaction_service.bulk_update_transactions(
        db_session, seed["org_id"], [a],
        category_id=seed["other_expense_id"], tags=["overflow"],
        actor_user_id=seed["user_id"],
    )
    assert updated == 1
    assert skipped == []  # category applied → row counted updated, not skipped
    row = await db_session.scalar(
        select(Transaction).options(selectinload(Transaction.tags)).where(Transaction.id == a)
    )
    assert row.category_id == seed["other_expense_id"]  # field change survived
    assert sorted(t.name_normalized for t in row.tags) == full  # tags unchanged
