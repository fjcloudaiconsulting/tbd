"""Service-level tests for promote_to_recurring.

Covers the atomic promotion flow that turns an existing transaction into
a recurring template:
  - happy path mirrors the tx fields onto the new template and links back
  - cross-org isolation
  - guards against transfer-leg promotion and double-promotion
  - server-side past-date guard
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization, Transaction
from app.models.base import Base
from app.models.category import CategoryType
from app.models.recurring import Frequency, RecurringTransaction
from app.models.transaction import TransactionStatus, TransactionType
from app.schemas.transaction import PromoteToRecurringRequest
from app.services import transaction_service
from app.services.exceptions import NotFoundError, ValidationError


# ── fixtures ───────────────────────────────────────────────────────────────

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
    org = Organization(name="Test", billing_cycle_day=1)
    db.add(org)
    await db.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db.add(at)
    await db.flush()
    a1 = Account(
        org_id=org.id, name="Acct A", account_type_id=at.id,
        balance=Decimal("1000"), currency="EUR",
    )
    a2 = Account(
        org_id=org.id, name="Acct B", account_type_id=at.id,
        balance=Decimal("0"), currency="EUR",
    )
    db.add_all([a1, a2])
    cat_groceries = Category(
        org_id=org.id, name="Groceries", slug="groceries",
        type=CategoryType.EXPENSE, is_system=False,
    )
    cat_transfer = Category(
        org_id=org.id, name="Transfer", slug="transfer",
        type=CategoryType.BOTH, is_system=True,
    )
    db.add_all([cat_groceries, cat_transfer])
    await db.flush()
    return {
        "org_id": org.id,
        "a1_id": a1.id,
        "a2_id": a2.id,
        "cat_groceries_id": cat_groceries.id,
        "cat_transfer_id": cat_transfer.id,
    }


async def _add_tx(
    db: AsyncSession,
    *,
    org_id: int,
    account_id: int,
    category_id: int,
    type: TransactionType = TransactionType.EXPENSE,
    amount: Decimal = Decimal("25.00"),
    description: str = "Coffee",
    on_date: date | None = None,
    linked_transaction_id: int | None = None,
    recurring_id: int | None = None,
) -> Transaction:
    tx = Transaction(
        org_id=org_id,
        account_id=account_id,
        category_id=category_id,
        description=description,
        amount=amount,
        type=type,
        status=TransactionStatus.SETTLED,
        date=on_date or date(2026, 5, 1),
        linked_transaction_id=linked_transaction_id,
        recurring_id=recurring_id,
        is_imported=False,
    )
    db.add(tx)
    await db.flush()
    return tx


# ── happy path ─────────────────────────────────────────────────────────────


async def test_promote_to_recurring_happy_path_creates_template_and_links(db_session):
    seed = await _seed(db_session)
    tx = await _add_tx(
        db_session,
        org_id=seed["org_id"], account_id=seed["a1_id"],
        category_id=seed["cat_groceries_id"],
        type=TransactionType.EXPENSE, amount=Decimal("12.50"),
        description="Weekly coffee",
    )
    await db_session.commit()

    body = PromoteToRecurringRequest(
        frequency="monthly",
        next_due_date=date.today() + timedelta(days=30),
    )
    result = await transaction_service.promote_to_recurring(
        db_session, seed["org_id"], tx.id, body
    )

    # Returned tx now has recurring_id populated.
    assert result.recurring_id is not None
    assert result.id == tx.id
    assert result.account_id == seed["a1_id"]

    # New RecurringTransaction is queryable and mirrors tx fields.
    rec = await db_session.scalar(
        select(RecurringTransaction).where(RecurringTransaction.id == result.recurring_id)
    )
    assert rec is not None
    assert rec.org_id == seed["org_id"]
    assert rec.account_id == seed["a1_id"]
    assert rec.category_id == seed["cat_groceries_id"]
    assert rec.description == "Weekly coffee"
    assert rec.amount == Decimal("12.50")
    assert rec.type == "expense"
    assert rec.frequency == Frequency.MONTHLY
    assert rec.next_due_date == date.today() + timedelta(days=30)
    assert rec.is_active is True
    assert rec.auto_settle is False


async def test_promote_to_recurring_today_is_allowed(db_session):
    seed = await _seed(db_session)
    tx = await _add_tx(
        db_session,
        org_id=seed["org_id"], account_id=seed["a1_id"],
        category_id=seed["cat_groceries_id"],
    )
    await db_session.commit()

    body = PromoteToRecurringRequest(
        frequency="weekly",
        next_due_date=date.today(),
    )
    result = await transaction_service.promote_to_recurring(
        db_session, seed["org_id"], tx.id, body
    )
    assert result.recurring_id is not None


# ── 404: not found / cross-org ────────────────────────────────────────────


async def test_promote_to_recurring_not_found(db_session):
    seed = await _seed(db_session)
    await db_session.commit()

    body = PromoteToRecurringRequest(
        frequency="monthly",
        next_due_date=date.today() + timedelta(days=30),
    )
    with pytest.raises(NotFoundError):
        await transaction_service.promote_to_recurring(
            db_session, seed["org_id"], 99999, body
        )


async def test_promote_to_recurring_cross_org_returns_not_found(db_session):
    seed = await _seed(db_session)
    # Second org with its own row.
    org2 = Organization(name="Other", billing_cycle_day=1)
    db_session.add(org2)
    await db_session.flush()
    at2 = AccountType(org_id=org2.id, name="Checking", slug="checking", is_system=True)
    db_session.add(at2)
    await db_session.flush()
    acct2 = Account(
        org_id=org2.id, name="X", account_type_id=at2.id,
        balance=Decimal("0"), currency="EUR",
    )
    cat2 = Category(
        org_id=org2.id, name="C2", slug="c2",
        type=CategoryType.EXPENSE, is_system=False,
    )
    db_session.add_all([acct2, cat2])
    await db_session.flush()
    tx_other = await _add_tx(
        db_session, org_id=org2.id, account_id=acct2.id, category_id=cat2.id,
    )
    await db_session.commit()

    body = PromoteToRecurringRequest(
        frequency="monthly",
        next_due_date=date.today() + timedelta(days=30),
    )
    # Caller passes org1's id but the tx belongs to org2 → looks like not found.
    with pytest.raises(NotFoundError):
        await transaction_service.promote_to_recurring(
            db_session, seed["org_id"], tx_other.id, body
        )

    # And no template was created in the foreign org.
    rec_count = await db_session.scalar(
        select(RecurringTransaction).where(RecurringTransaction.org_id == org2.id)
    )
    assert rec_count is None


# ── 400: already promoted ──────────────────────────────────────────────────


async def test_promote_to_recurring_rejects_already_linked(db_session):
    seed = await _seed(db_session)

    # Pre-create a recurring template, point an existing tx at it.
    tmpl = RecurringTransaction(
        org_id=seed["org_id"], account_id=seed["a1_id"],
        category_id=seed["cat_groceries_id"], description="x",
        amount=Decimal("5"), type="expense", frequency=Frequency.MONTHLY,
        next_due_date=date.today(), auto_settle=False, is_active=True,
    )
    db_session.add(tmpl)
    await db_session.flush()
    tx = await _add_tx(
        db_session,
        org_id=seed["org_id"], account_id=seed["a1_id"],
        category_id=seed["cat_groceries_id"], recurring_id=tmpl.id,
    )
    await db_session.commit()

    body = PromoteToRecurringRequest(
        frequency="monthly",
        next_due_date=date.today() + timedelta(days=30),
    )
    with pytest.raises(ValidationError) as exc:
        await transaction_service.promote_to_recurring(
            db_session, seed["org_id"], tx.id, body
        )
    assert "already" in exc.value.detail.lower()

    # No second template was created.
    rows = (
        await db_session.execute(
            select(RecurringTransaction).where(RecurringTransaction.org_id == seed["org_id"])
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == tmpl.id


# ── 400: transfer-leg ─────────────────────────────────────────────────────


async def test_promote_to_recurring_rejects_transfer_leg(db_session):
    seed = await _seed(db_session)

    # Stage a paired transfer (two rows linked bidirectionally).
    expense_leg = await _add_tx(
        db_session,
        org_id=seed["org_id"], account_id=seed["a1_id"],
        category_id=seed["cat_transfer_id"],
        type=TransactionType.EXPENSE, amount=Decimal("50"),
        description="xfer out",
    )
    income_leg = await _add_tx(
        db_session,
        org_id=seed["org_id"], account_id=seed["a2_id"],
        category_id=seed["cat_transfer_id"],
        type=TransactionType.INCOME, amount=Decimal("50"),
        description="xfer in",
    )
    expense_leg.linked_transaction_id = income_leg.id
    income_leg.linked_transaction_id = expense_leg.id
    await db_session.commit()

    body = PromoteToRecurringRequest(
        frequency="monthly",
        next_due_date=date.today() + timedelta(days=30),
    )
    with pytest.raises(ValidationError) as exc:
        await transaction_service.promote_to_recurring(
            db_session, seed["org_id"], expense_leg.id, body
        )
    assert "transfer" in exc.value.detail.lower()


# ── 400: past date (server-side guard) ─────────────────────────────────────


async def test_promote_to_recurring_rejects_past_date_at_service_layer(db_session):
    """Service-side guard: when callers bypass the schema (e.g. internal
    reuse), the past-date semantic must still hold."""
    seed = await _seed(db_session)
    tx = await _add_tx(
        db_session,
        org_id=seed["org_id"], account_id=seed["a1_id"],
        category_id=seed["cat_groceries_id"],
    )
    await db_session.commit()

    # Build the request via model_construct to bypass field validators.
    body = PromoteToRecurringRequest.model_construct(
        frequency="monthly",
        next_due_date=date.today() - timedelta(days=1),
    )
    with pytest.raises(ValidationError) as exc:
        await transaction_service.promote_to_recurring(
            db_session, seed["org_id"], tx.id, body
        )
    assert "today or later" in exc.value.detail.lower()


# ── frequency forwarding spot-check ────────────────────────────────────────


@pytest.mark.parametrize(
    "freq",
    ["weekly", "biweekly", "monthly", "quarterly", "yearly"],
)
async def test_promote_to_recurring_forwards_each_frequency(db_session, freq):
    seed = await _seed(db_session)
    tx = await _add_tx(
        db_session,
        org_id=seed["org_id"], account_id=seed["a1_id"],
        category_id=seed["cat_groceries_id"],
    )
    await db_session.commit()

    body = PromoteToRecurringRequest(
        frequency=freq,
        next_due_date=date.today() + timedelta(days=30),
    )
    result = await transaction_service.promote_to_recurring(
        db_session, seed["org_id"], tx.id, body
    )
    rec = await db_session.scalar(
        select(RecurringTransaction).where(RecurringTransaction.id == result.recurring_id)
    )
    assert rec is not None
    assert rec.frequency == Frequency(freq)
