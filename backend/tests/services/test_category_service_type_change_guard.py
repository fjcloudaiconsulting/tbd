"""Server-side guard: changing a Category.type must not retroactively
break existing references (transactions, recurring templates, forecast
plan items, transfer legs).

Closes the third HIGH finding from PR #150 review: PUT /api/v1/categories/{id}
let cat.type be reassigned freely, bypassing every (type, category)
compatibility guard added in the prior commits.

Rule: a type change OLD -> NEW is rejected if any existing row referencing
this category (or any of its children, when the category is a master) is
incompatible with NEW under the same compatibility semantics that
validate_category_for_type uses.

Special case: BOTH categories that are referenced by a transfer leg cannot
be moved off BOTH at all (transfer pairs structurally need both directions).
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
from app.models.billing import BillingPeriod
from app.models.category import CategoryType
from app.models.forecast_plan import (
    ForecastItemType,
    ForecastPlan,
    ForecastPlanItem,
    ItemSource,
    PlanStatus,
)
from app.models.recurring import Frequency, RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.services import category_service
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


async def _seed_org(db: AsyncSession) -> dict:
    """Org + one account + one EXPENSE master + one BOTH master."""
    org = Organization(name="T", billing_cycle_day=1)
    db.add(org)
    await db.flush()
    at = AccountType(
        org_id=org.id, name="Checking", slug="checking", is_system=True,
    )
    db.add(at)
    await db.flush()
    acct = Account(
        org_id=org.id, name="Main", account_type_id=at.id,
        balance=Decimal("0"), currency="EUR",
    )
    db.add(acct)
    await db.flush()
    expense_master = Category(
        org_id=org.id, name="Groceries", slug="groceries",
        type=CategoryType.EXPENSE,
    )
    both_master = Category(
        org_id=org.id, name="Flex", slug="flex", type=CategoryType.BOTH,
    )
    db.add_all([expense_master, both_master])
    await db.flush()
    return {
        "org_id": org.id,
        "acct_id": acct.id,
        "expense_master": expense_master,
        "both_master": both_master,
    }


# ── Pure helper semantics ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_change_skips_guard(db_session: AsyncSession) -> None:
    """OLD == NEW: helper is a no-op; no rejection regardless of references."""
    seed = await _seed_org(db_session)
    cat = seed["expense_master"]
    # No references either way.
    await category_service.validate_category_type_change(
        db_session, cat, CategoryType.EXPENSE,
    )


@pytest.mark.asyncio
async def test_change_to_both_always_safe(db_session: AsyncSession) -> None:
    """EXPENSE -> BOTH and INCOME -> BOTH are always safe, regardless of refs."""
    seed = await _seed_org(db_session)
    cat = seed["expense_master"]
    db_session.add(Transaction(
        org_id=seed["org_id"], account_id=seed["acct_id"],
        category_id=cat.id, description="x", amount=Decimal("10"),
        type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
        date=date(2026, 5, 1), settled_date=date(2026, 5, 1),
    ))
    await db_session.commit()
    await category_service.validate_category_type_change(
        db_session, cat, CategoryType.BOTH,
    )


@pytest.mark.asyncio
async def test_change_with_no_references_succeeds(db_session: AsyncSession) -> None:
    """EXPENSE -> INCOME with no references: no rows to break, safe."""
    seed = await _seed_org(db_session)
    cat = seed["expense_master"]
    await category_service.validate_category_type_change(
        db_session, cat, CategoryType.INCOME,
    )


# ── Transaction references ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_change_rejected_when_expense_transaction_blocks(
    db_session: AsyncSession,
) -> None:
    """EXPENSE -> INCOME with one expense txn: rejected."""
    seed = await _seed_org(db_session)
    cat = seed["expense_master"]
    db_session.add(Transaction(
        org_id=seed["org_id"], account_id=seed["acct_id"],
        category_id=cat.id, description="x", amount=Decimal("10"),
        type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
        date=date(2026, 5, 1), settled_date=date(2026, 5, 1),
    ))
    await db_session.commit()
    with pytest.raises(ValidationError):
        await category_service.validate_category_type_change(
            db_session, cat, CategoryType.INCOME,
        )


@pytest.mark.asyncio
async def test_both_to_expense_rejected_when_income_txn_exists(
    db_session: AsyncSession,
) -> None:
    seed = await _seed_org(db_session)
    cat = seed["both_master"]
    db_session.add(Transaction(
        org_id=seed["org_id"], account_id=seed["acct_id"],
        category_id=cat.id, description="pay", amount=Decimal("100"),
        type=TransactionType.INCOME, status=TransactionStatus.SETTLED,
        date=date(2026, 5, 1), settled_date=date(2026, 5, 1),
    ))
    await db_session.commit()
    with pytest.raises(ValidationError):
        await category_service.validate_category_type_change(
            db_session, cat, CategoryType.EXPENSE,
        )


@pytest.mark.asyncio
async def test_both_to_income_rejected_when_expense_txn_exists(
    db_session: AsyncSession,
) -> None:
    seed = await _seed_org(db_session)
    cat = seed["both_master"]
    db_session.add(Transaction(
        org_id=seed["org_id"], account_id=seed["acct_id"],
        category_id=cat.id, description="buy", amount=Decimal("10"),
        type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
        date=date(2026, 5, 1), settled_date=date(2026, 5, 1),
    ))
    await db_session.commit()
    with pytest.raises(ValidationError):
        await category_service.validate_category_type_change(
            db_session, cat, CategoryType.INCOME,
        )


@pytest.mark.asyncio
async def test_both_to_expense_with_no_references_succeeds(
    db_session: AsyncSession,
) -> None:
    seed = await _seed_org(db_session)
    cat = seed["both_master"]
    await category_service.validate_category_type_change(
        db_session, cat, CategoryType.EXPENSE,
    )


# ── Recurring template references ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_change_rejected_by_recurring_template(
    db_session: AsyncSession,
) -> None:
    """BOTH -> EXPENSE with an income-typed recurring template: rejected."""
    seed = await _seed_org(db_session)
    cat = seed["both_master"]
    db_session.add(RecurringTransaction(
        org_id=seed["org_id"], account_id=seed["acct_id"],
        category_id=cat.id, description="paycheck",
        amount=Decimal("100"), type="income",
        frequency=Frequency.MONTHLY, next_due_date=date(2026, 6, 1),
    ))
    await db_session.commit()
    with pytest.raises(ValidationError):
        await category_service.validate_category_type_change(
            db_session, cat, CategoryType.EXPENSE,
        )


# ── Forecast plan item references ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_change_rejected_by_forecast_plan_item(
    db_session: AsyncSession,
) -> None:
    """BOTH -> INCOME with an expense forecast item: rejected."""
    seed = await _seed_org(db_session)
    cat = seed["both_master"]
    period = BillingPeriod(
        org_id=seed["org_id"], start_date=date(2026, 5, 1),
        end_date=date(2026, 5, 31),
    )
    db_session.add(period)
    await db_session.flush()
    plan = ForecastPlan(
        org_id=seed["org_id"], billing_period_id=period.id,
        status=PlanStatus.DRAFT,
    )
    db_session.add(plan)
    await db_session.flush()
    db_session.add(ForecastPlanItem(
        plan_id=plan.id, org_id=seed["org_id"], category_id=cat.id,
        type=ForecastItemType.EXPENSE, planned_amount=Decimal("50"),
        source=ItemSource.MANUAL,
    ))
    await db_session.commit()
    with pytest.raises(ValidationError):
        await category_service.validate_category_type_change(
            db_session, cat, CategoryType.INCOME,
        )


# ── Transfer leg lockdown ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_transfer_leg_blocks_any_move_off_both(
    db_session: AsyncSession,
) -> None:
    """A BOTH category referenced by a transfer leg cannot move to EXPENSE
    or INCOME — transfer pairs require BOTH on both legs."""
    seed = await _seed_org(db_session)
    # Second account so transfer legs reference different accounts.
    acct2 = Account(
        org_id=seed["org_id"], name="Other",
        account_type_id=(await db_session.scalar(
            __import__("sqlalchemy").select(AccountType.id).where(
                AccountType.org_id == seed["org_id"]
            )
        )),
        balance=Decimal("0"), currency="EUR",
    )
    db_session.add(acct2)
    await db_session.flush()
    cat = seed["both_master"]
    leg_out = Transaction(
        org_id=seed["org_id"], account_id=seed["acct_id"],
        category_id=cat.id, description="xfer", amount=Decimal("50"),
        type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
        date=date(2026, 5, 1), settled_date=date(2026, 5, 1),
    )
    leg_in = Transaction(
        org_id=seed["org_id"], account_id=acct2.id,
        category_id=cat.id, description="xfer", amount=Decimal("50"),
        type=TransactionType.INCOME, status=TransactionStatus.SETTLED,
        date=date(2026, 5, 1), settled_date=date(2026, 5, 1),
    )
    db_session.add_all([leg_out, leg_in])
    await db_session.flush()
    leg_out.linked_transaction_id = leg_in.id
    leg_in.linked_transaction_id = leg_out.id
    await db_session.commit()
    with pytest.raises(ValidationError):
        await category_service.validate_category_type_change(
            db_session, cat, CategoryType.EXPENSE,
        )
    with pytest.raises(ValidationError):
        await category_service.validate_category_type_change(
            db_session, cat, CategoryType.INCOME,
        )


# ── Master category recursion ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_master_change_rejected_by_child_reference(
    db_session: AsyncSession,
) -> None:
    """Changing a master's type must consider every child's references too.

    A child currently typed EXPENSE under a BOTH master, with an expense
    transaction tagged on the child, blocks the master from moving to INCOME.
    """
    seed = await _seed_org(db_session)
    master = seed["both_master"]
    child = Category(
        org_id=seed["org_id"], parent_id=master.id, name="Sub",
        type=CategoryType.EXPENSE,
    )
    db_session.add(child)
    await db_session.flush()
    db_session.add(Transaction(
        org_id=seed["org_id"], account_id=seed["acct_id"],
        category_id=child.id, description="x", amount=Decimal("10"),
        type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
        date=date(2026, 5, 1), settled_date=date(2026, 5, 1),
    ))
    await db_session.commit()
    with pytest.raises(ValidationError):
        await category_service.validate_category_type_change(
            db_session, master, CategoryType.INCOME,
        )


@pytest.mark.asyncio
async def test_master_change_succeeds_when_children_compatible(
    db_session: AsyncSession,
) -> None:
    """A master EXPENSE -> BOTH succeeds even with child + child txn refs."""
    seed = await _seed_org(db_session)
    master = seed["expense_master"]
    child = Category(
        org_id=seed["org_id"], parent_id=master.id, name="Sub",
        type=CategoryType.EXPENSE,
    )
    db_session.add(child)
    await db_session.flush()
    db_session.add(Transaction(
        org_id=seed["org_id"], account_id=seed["acct_id"],
        category_id=child.id, description="x", amount=Decimal("10"),
        type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
        date=date(2026, 5, 1), settled_date=date(2026, 5, 1),
    ))
    await db_session.commit()
    await category_service.validate_category_type_change(
        db_session, master, CategoryType.BOTH,
    )
