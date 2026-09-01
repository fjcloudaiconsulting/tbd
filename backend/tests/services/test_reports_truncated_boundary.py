"""``meta.truncated`` boundary fences for every reports source (TBD-484).

``truncated`` means "there was MORE than we returned". Three sources
computed it as ``len(out_rows) >= limit`` over rows the DATABASE had
already limited, so the flag could never be anything but a restatement
of "the result filled the limit" — a **false positive on complete
data**. TBD-430 makes the flag visible (a loud notice, and suppression
of the totals row / donut-hole total), so a complete 10-row result at
``limit=10`` hid a correct total and told the user it was incomplete.

⚠ The whole defect is an off-by-one, so every fence below drives BOTH
``exactly limit`` and ``limit + 1`` rows-available. A fence that only
drives an obviously-over-limit case (50 rows at ``limit=10``) passes
against the broken ``>=`` code too and proves nothing.

The two sources that were already correct (credit_utilization, networth
— both measure pre-slice, in Python) are fenced at the same boundary so
a later "let's make all five consistent" refactor onto ``>=`` is caught
rather than silently re-introducing the defect.
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

from app.models.base import Base
from app.models import Account, AccountType, Category, Organization
from app.models.category import CategoryType
from app.models.recurring import RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.reports import sources as registry
from app.schemas.reports_query import (
    Aggregation,
    Dataset,
    Dimension,
    Measure,
    MeasureField,
    ReportsQuery,
)

# Small enough to seed exactly, large enough that the ORDER BY has work
# to do. The defect is scale-free: it fires at ANY limit.
LIMIT = 3


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


async def _org(db, *, type_slug="bank", type_name="Bank"):
    org = Organization(name="Org", billing_cycle_day=1)
    db.add(org)
    await db.flush()
    at = AccountType(org_id=org.id, name=type_name, slug=type_slug, is_system=False)
    db.add(at)
    await db.flush()
    return org, at


# ─── transactions (reports_query_service.execute_query) ─────────────


async def _seed_transactions(db, n_groups: int) -> int:
    """One settled expense per category, ``n_groups`` distinct categories.

    Descending, distinct amounts so the default ``ORDER BY value DESC``
    is total — no tie can make the kept set ambiguous.
    """
    org, at = await _org(db)
    acct = Account(
        org_id=org.id, name="Chk", account_type_id=at.id,
        balance=Decimal("0"), currency="EUR", is_active=True,
    )
    db.add(acct)
    await db.flush()
    day = date(2026, 5, 15)
    for i in range(n_groups):
        cat = Category(org_id=org.id, name=f"Cat{i:02d}", type=CategoryType.EXPENSE)
        db.add(cat)
        await db.flush()
        db.add(
            Transaction(
                org_id=org.id, account_id=acct.id, category_id=cat.id,
                description="row", amount=Decimal(str(1000 - i)),
                type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
                date=day, settled_date=day,
            )
        )
    await db.flush()
    return org.id


def _transactions_ast() -> ReportsQuery:
    return ReportsQuery(
        dataset=Dataset.TRANSACTIONS,
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
        dimensions=[Dimension.CATEGORY],
        limit=LIMIT,
    )


@pytest.mark.asyncio
async def test_transactions_exactly_limit_is_not_truncated(db_session):
    """A COMPLETE result that exactly fills the limit is NOT truncated.

    Kills ``len(out_rows) >= ast.limit``: the DB already applied
    ``LIMIT n``, so that predicate is true for every full page.
    """
    org_id = await _seed_transactions(db_session, LIMIT)
    src = registry.get_source("transactions")
    rows, meta = await src.build_rows(db_session, org_id, _transactions_ast())

    assert len(rows) == LIMIT
    assert meta["row_count"] == LIMIT
    assert meta["truncated"] is False


@pytest.mark.asyncio
async def test_transactions_one_over_limit_is_truncated(db_session):
    """One more row than the limit → truncated, and the extra probe row
    never reaches the payload or ``row_count``."""
    org_id = await _seed_transactions(db_session, LIMIT + 1)
    src = registry.get_source("transactions")
    rows, meta = await src.build_rows(db_session, org_id, _transactions_ast())

    assert len(rows) == LIMIT
    assert meta["row_count"] == LIMIT
    assert meta["truncated"] is True


# ─── accounts ───────────────────────────────────────────────────────


async def _seed_accounts(db, n_groups: int) -> int:
    org, at = await _org(db)
    for i in range(n_groups):
        db.add(
            Account(
                org_id=org.id, name=f"Acct{i:02d}", account_type_id=at.id,
                balance=Decimal(str(1000 - i)), currency="EUR", is_active=True,
            )
        )
    await db.flush()
    return org.id


def _accounts_ast() -> ReportsQuery:
    return ReportsQuery(
        dataset=Dataset.ACCOUNTS,
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.BALANCE),
        dimensions=[Dimension.ACCOUNT],
        limit=LIMIT,
    )


@pytest.mark.asyncio
async def test_accounts_exactly_limit_is_not_truncated(db_session):
    org_id = await _seed_accounts(db_session, LIMIT)
    src = registry.get_source("accounts")
    rows, meta = await src.build_rows(db_session, org_id, _accounts_ast())

    assert len(rows) == LIMIT
    assert meta["row_count"] == LIMIT
    assert meta["truncated"] is False


@pytest.mark.asyncio
async def test_accounts_one_over_limit_is_truncated(db_session):
    org_id = await _seed_accounts(db_session, LIMIT + 1)
    src = registry.get_source("accounts")
    rows, meta = await src.build_rows(db_session, org_id, _accounts_ast())

    assert len(rows) == LIMIT
    assert meta["row_count"] == LIMIT
    assert meta["truncated"] is True


# ─── recurring ──────────────────────────────────────────────────────


async def _seed_recurring(db, n_groups: int) -> int:
    org, at = await _org(db)
    acct = Account(
        org_id=org.id, name="Chk", account_type_id=at.id,
        balance=Decimal("0"), currency="EUR", is_active=True,
    )
    db.add(acct)
    await db.flush()
    for i in range(n_groups):
        cat = Category(org_id=org.id, name=f"Cat{i:02d}", type=CategoryType.EXPENSE)
        db.add(cat)
        await db.flush()
        db.add(
            RecurringTransaction(
                org_id=org.id, account_id=acct.id, category_id=cat.id,
                description=f"tpl{i:02d}", amount=Decimal(str(1000 - i)),
                type="expense", frequency="monthly",
                next_due_date=date(2026, 1, 1), is_active=True,
            )
        )
    await db.flush()
    return org.id


def _recurring_ast() -> ReportsQuery:
    return ReportsQuery(
        dataset=Dataset.RECURRING,
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
        dimensions=[Dimension.CATEGORY],
        limit=LIMIT,
    )


@pytest.mark.asyncio
async def test_recurring_exactly_limit_is_not_truncated(db_session):
    org_id = await _seed_recurring(db_session, LIMIT)
    src = registry.get_source("recurring")
    rows, meta = await src.build_rows(db_session, org_id, _recurring_ast())

    assert len(rows) == LIMIT
    assert meta["row_count"] == LIMIT
    assert meta["truncated"] is False


@pytest.mark.asyncio
async def test_recurring_one_over_limit_is_truncated(db_session):
    org_id = await _seed_recurring(db_session, LIMIT + 1)
    src = registry.get_source("recurring")
    rows, meta = await src.build_rows(db_session, org_id, _recurring_ast())

    assert len(rows) == LIMIT
    assert meta["row_count"] == LIMIT
    assert meta["truncated"] is True


# ─── credit_utilization (already correct — regression fence) ────────


async def _seed_credit_cards(db, n_groups: int) -> int:
    org, at = await _org(db, type_slug="credit_card", type_name="Credit Card")
    for i in range(n_groups):
        db.add(
            Account(
                org_id=org.id, name=f"Card{i:02d}", account_type_id=at.id,
                balance=Decimal(str(-(1000 - i))), currency="EUR", is_active=True,
                credit_limit=Decimal("5000"),
            )
        )
    await db.flush()
    return org.id


def _credit_ast() -> ReportsQuery:
    return ReportsQuery(
        dataset=Dataset.CREDIT_UTILIZATION,
        measure=Measure(agg=Aggregation.AVG, field=MeasureField.UTILIZATION_PCT),
        dimensions=[Dimension.ACCOUNT],
        limit=LIMIT,
    )


@pytest.mark.asyncio
async def test_credit_utilization_exactly_limit_is_not_truncated(db_session):
    """Regression fence. This source measures pre-slice (``> limit``) and
    is CORRECT today — the fence exists so a "make all five consistent"
    refactor onto ``>=`` is caught here rather than in production."""
    org_id = await _seed_credit_cards(db_session, LIMIT)
    src = registry.get_source("credit_utilization")
    rows, meta = await src.build_rows(db_session, org_id, _credit_ast())

    assert len(rows) == LIMIT
    assert meta["row_count"] == LIMIT
    assert meta["truncated"] is False


@pytest.mark.asyncio
async def test_credit_utilization_one_over_limit_is_truncated(db_session):
    org_id = await _seed_credit_cards(db_session, LIMIT + 1)
    src = registry.get_source("credit_utilization")
    rows, meta = await src.build_rows(db_session, org_id, _credit_ast())

    assert len(rows) == LIMIT
    assert meta["row_count"] == LIMIT
    assert meta["truncated"] is True


# ─── networth (already correct — regression fence) ──────────────────


async def _seed_networth_months(db, n_months: int) -> int:
    """Opening balance in 2026-01 plus one settled txn per following
    month, so the series has exactly ``n_months`` buckets."""
    org, at = await _org(db)
    cat = Category(org_id=org.id, name="Misc", type=CategoryType.EXPENSE)
    db.add(cat)
    await db.flush()
    acct = Account(
        org_id=org.id, name="Chk", account_type_id=at.id,
        balance=Decimal("1000"), currency="EUR", is_active=True,
        opening_balance=Decimal("1000"), opening_balance_date=date(2026, 1, 5),
    )
    db.add(acct)
    await db.flush()
    for m in range(2, n_months + 1):  # Jan is the opening-balance bucket
        db.add(
            Transaction(
                org_id=org.id, account_id=acct.id, category_id=cat.id,
                description="row", amount=Decimal("10"),
                type=TransactionType.INCOME, status=TransactionStatus.SETTLED,
                date=date(2026, m, 10), settled_date=date(2026, m, 10),
            )
        )
    await db.flush()
    return org.id


def _networth_ast() -> ReportsQuery:
    return ReportsQuery(
        dataset=Dataset.NETWORTH,
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.NET_WORTH),
        dimensions=[Dimension.MONTH],
        limit=LIMIT,
    )


@pytest.mark.asyncio
async def test_networth_exactly_limit_is_not_truncated(db_session):
    """Regression fence — see the credit_utilization note above."""
    org_id = await _seed_networth_months(db_session, LIMIT)
    src = registry.get_source("networth")
    rows, meta = await src.build_rows(db_session, org_id, _networth_ast())

    assert len(rows) == LIMIT
    assert meta["row_count"] == LIMIT
    assert meta["truncated"] is False


@pytest.mark.asyncio
async def test_networth_one_over_limit_is_truncated(db_session):
    org_id = await _seed_networth_months(db_session, LIMIT + 1)
    src = registry.get_source("networth")
    rows, meta = await src.build_rows(db_session, org_id, _networth_ast())

    assert len(rows) == LIMIT
    assert meta["row_count"] == LIMIT
    assert meta["truncated"] is True
