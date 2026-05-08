"""Tests for account_balance_forecast_service.compute_account_balance_forecast.

Pins the spec contract for /api/v1/forecast/account-balances:

  expected_account_month_end_balance = stored balance + pending delta in period

Settled rows are NOT added (already in stored balance — would double-count).
Pending transfer legs ARE included (per-account balance math).
Manual adjustments are excluded (settled-only today, defensive filter).
Pending outside the selected period is excluded.
Effective period date = COALESCE(settled_date, date).
Totals are grouped by currency.
"""
import datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import (
    Account,
    AccountType,
    Category,
    Organization,
    Transaction,
)
from app.models.base import Base
from app.models.billing import BillingPeriod
from app.models.category import CategoryType
from app.models.transaction import TransactionStatus, TransactionType
from app.services.account_balance_forecast_service import (
    compute_account_balance_forecast,
)


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


PERIOD_START = datetime.date(2026, 5, 1)
PERIOD_END = datetime.date(2026, 5, 31)
IN_PERIOD = datetime.date(2026, 5, 15)
BEFORE_PERIOD = datetime.date(2026, 4, 20)
AFTER_PERIOD = datetime.date(2026, 6, 5)


async def _seed(
    db: AsyncSession,
    *,
    second_currency: bool = False,
    second_account_currency: str = "USD",
):
    org = Organization(name="Test", billing_cycle_day=1)
    db.add(org)
    await db.flush()

    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db.add(at)
    await db.flush()

    accounts: dict[str, Account] = {}
    accounts["primary"] = Account(
        org_id=org.id,
        name="Checking",
        account_type_id=at.id,
        balance=Decimal("1000.00"),
        currency="EUR",
        is_default=True,
    )
    accounts["secondary"] = Account(
        org_id=org.id,
        name="Savings",
        account_type_id=at.id,
        balance=Decimal("5000.00"),
        currency="EUR",
        is_default=False,
    )
    db.add_all([accounts["primary"], accounts["secondary"]])
    await db.flush()

    if second_currency:
        accounts["usd"] = Account(
            org_id=org.id,
            name="USD Cash",
            account_type_id=at.id,
            balance=Decimal("200.00"),
            currency=second_account_currency,
            is_default=False,
        )
        db.add(accounts["usd"])
        await db.flush()

    cat_income = Category(
        org_id=org.id, name="Salary", slug="salary", type=CategoryType.INCOME
    )
    cat_expense = Category(
        org_id=org.id, name="Groceries", slug="groceries", type=CategoryType.EXPENSE
    )
    cat_transfer = Category(
        org_id=org.id, name="Transfer", slug="transfer", type=CategoryType.BOTH,
        is_system=True,
    )
    db.add_all([cat_income, cat_expense, cat_transfer])
    await db.flush()

    period = BillingPeriod(
        org_id=org.id, start_date=PERIOD_START, end_date=PERIOD_END
    )
    db.add(period)
    await db.flush()

    return {
        "org_id": org.id,
        "accounts": accounts,
        "cat_income": cat_income.id,
        "cat_expense": cat_expense.id,
        "cat_transfer": cat_transfer.id,
        "period": period,
    }


def _new_tx(**overrides) -> Transaction:
    """Build a Transaction with sensible defaults for these tests."""
    defaults = dict(
        amount=Decimal("100.00"),
        type=TransactionType.EXPENSE,
        status=TransactionStatus.PENDING,
        date=IN_PERIOD,
        settled_date=None,
        description="x",
        is_imported=False,
        is_manual_adjustment=False,
    )
    defaults.update(overrides)
    return Transaction(**defaults)


# ---------- Test 1: settled rows are not double-counted ----------

async def test_settled_transactions_not_double_counted(db_session: AsyncSession):
    """Account balance is authoritative. Settled rows must not be added on
    top of stored balance; pending delta only."""
    seed = await _seed(db_session)
    primary = seed["accounts"]["primary"]

    # Settled rows in-period — these should be ignored entirely (already in
    # the stored balance). If the service double-counted, the expected
    # would deviate from the stored balance.
    db_session.add_all(
        [
            _new_tx(
                org_id=seed["org_id"],
                account_id=primary.id,
                category_id=seed["cat_expense"],
                amount=Decimal("250.00"),
                type=TransactionType.EXPENSE,
                status=TransactionStatus.SETTLED,
                date=IN_PERIOD,
                settled_date=IN_PERIOD,
            ),
            _new_tx(
                org_id=seed["org_id"],
                account_id=primary.id,
                category_id=seed["cat_income"],
                amount=Decimal("400.00"),
                type=TransactionType.INCOME,
                status=TransactionStatus.SETTLED,
                date=IN_PERIOD,
                settled_date=IN_PERIOD,
            ),
        ]
    )
    await db_session.commit()

    result = await compute_account_balance_forecast(
        db_session, seed["org_id"], period_start=PERIOD_START
    )

    primary_row = next(a for a in result["accounts"] if a["account_id"] == primary.id)
    assert primary_row["balance"] == "1000.00"
    assert primary_row["pending_delta"] == "0.00"
    assert primary_row["expected_month_end_balance"] == "1000.00"


# ---------- Test 2: totals grouped by currency ----------

async def test_totals_grouped_by_currency(db_session: AsyncSession):
    seed = await _seed(db_session, second_currency=True)
    eur_primary = seed["accounts"]["primary"]
    eur_secondary = seed["accounts"]["secondary"]
    usd_account = seed["accounts"]["usd"]

    # Pending expense on USD account
    db_session.add(
        _new_tx(
            org_id=seed["org_id"],
            account_id=usd_account.id,
            category_id=seed["cat_expense"],
            amount=Decimal("50.00"),
            type=TransactionType.EXPENSE,
            status=TransactionStatus.PENDING,
            date=IN_PERIOD,
        )
    )
    await db_session.commit()

    result = await compute_account_balance_forecast(
        db_session, seed["org_id"], period_start=PERIOD_START
    )

    by_currency = {t["currency"]: t for t in result["totals"]}
    assert set(by_currency.keys()) == {"EUR", "USD"}
    assert by_currency["EUR"]["balance"] == str(
        (Decimal(str(eur_primary.balance)) + Decimal(str(eur_secondary.balance))).quantize(Decimal("0.01"))
    )
    assert by_currency["EUR"]["pending_delta"] == "0.00"
    assert by_currency["EUR"]["expected_month_end_balance"] == "6000.00"
    assert by_currency["USD"]["balance"] == "200.00"
    assert by_currency["USD"]["pending_delta"] == "-50.00"
    assert by_currency["USD"]["expected_month_end_balance"] == "150.00"


# ---------- Test 3: pending expense lowers expected balance ----------

async def test_pending_expense_lowers_expected(db_session: AsyncSession):
    seed = await _seed(db_session)
    primary = seed["accounts"]["primary"]

    db_session.add(
        _new_tx(
            org_id=seed["org_id"],
            account_id=primary.id,
            category_id=seed["cat_expense"],
            amount=Decimal("75.00"),
            type=TransactionType.EXPENSE,
            status=TransactionStatus.PENDING,
            date=IN_PERIOD,
        )
    )
    await db_session.commit()

    result = await compute_account_balance_forecast(
        db_session, seed["org_id"], period_start=PERIOD_START
    )

    row = next(a for a in result["accounts"] if a["account_id"] == primary.id)
    assert row["pending_delta"] == "-75.00"
    assert row["expected_month_end_balance"] == "925.00"


# ---------- Test 4: pending income raises expected balance ----------

async def test_pending_income_raises_expected(db_session: AsyncSession):
    seed = await _seed(db_session)
    primary = seed["accounts"]["primary"]

    db_session.add(
        _new_tx(
            org_id=seed["org_id"],
            account_id=primary.id,
            category_id=seed["cat_income"],
            amount=Decimal("250.00"),
            type=TransactionType.INCOME,
            status=TransactionStatus.PENDING,
            date=IN_PERIOD,
        )
    )
    await db_session.commit()

    result = await compute_account_balance_forecast(
        db_session, seed["org_id"], period_start=PERIOD_START
    )

    row = next(a for a in result["accounts"] if a["account_id"] == primary.id)
    assert row["pending_delta"] == "250.00"
    assert row["expected_month_end_balance"] == "1250.00"


# ---------- Test 5: pending transfer pair lowers source / raises destination ----------

async def test_pending_transfer_pair_moves_balances(db_session: AsyncSession):
    """Per-account math must include pending transfer legs even though they
    aren't reportable income/expense."""
    seed = await _seed(db_session)
    src = seed["accounts"]["primary"]
    dst = seed["accounts"]["secondary"]

    expense_leg = _new_tx(
        org_id=seed["org_id"],
        account_id=src.id,
        category_id=seed["cat_transfer"],
        amount=Decimal("400.00"),
        type=TransactionType.EXPENSE,
        status=TransactionStatus.PENDING,
        date=IN_PERIOD,
    )
    income_leg = _new_tx(
        org_id=seed["org_id"],
        account_id=dst.id,
        category_id=seed["cat_transfer"],
        amount=Decimal("400.00"),
        type=TransactionType.INCOME,
        status=TransactionStatus.PENDING,
        date=IN_PERIOD,
    )
    db_session.add_all([expense_leg, income_leg])
    await db_session.flush()
    expense_leg.linked_transaction_id = income_leg.id
    income_leg.linked_transaction_id = expense_leg.id
    await db_session.commit()

    result = await compute_account_balance_forecast(
        db_session, seed["org_id"], period_start=PERIOD_START
    )

    by_id = {a["account_id"]: a for a in result["accounts"]}
    assert by_id[src.id]["pending_delta"] == "-400.00"
    assert by_id[src.id]["expected_month_end_balance"] == "600.00"
    assert by_id[dst.id]["pending_delta"] == "400.00"
    assert by_id[dst.id]["expected_month_end_balance"] == "5400.00"


# ---------- Test 6: pending outside selected period is excluded ----------

async def test_pending_outside_period_excluded(db_session: AsyncSession):
    seed = await _seed(db_session)
    primary = seed["accounts"]["primary"]

    db_session.add_all(
        [
            _new_tx(
                org_id=seed["org_id"],
                account_id=primary.id,
                category_id=seed["cat_expense"],
                amount=Decimal("99.00"),
                type=TransactionType.EXPENSE,
                status=TransactionStatus.PENDING,
                date=BEFORE_PERIOD,
            ),
            _new_tx(
                org_id=seed["org_id"],
                account_id=primary.id,
                category_id=seed["cat_expense"],
                amount=Decimal("88.00"),
                type=TransactionType.EXPENSE,
                status=TransactionStatus.PENDING,
                date=AFTER_PERIOD,
            ),
        ]
    )
    await db_session.commit()

    result = await compute_account_balance_forecast(
        db_session, seed["org_id"], period_start=PERIOD_START
    )

    row = next(a for a in result["accounts"] if a["account_id"] == primary.id)
    assert row["pending_delta"] == "0.00"
    assert row["expected_month_end_balance"] == "1000.00"


# ---------- Test 7: settled_date is preferred over date for period bucketing ----------

async def test_effective_period_date_uses_settled_date_then_date(
    db_session: AsyncSession,
):
    """Pending with settled_date estimate uses settled_date.
    Pending without settled_date falls back to date."""
    seed = await _seed(db_session)
    primary = seed["accounts"]["primary"]

    # (a) date is BEFORE period, but settled_date estimate is IN period -> include
    # (b) date is IN period, no settled_date -> include
    # (c) date is IN period, but settled_date is AFTER period -> exclude
    db_session.add_all(
        [
            _new_tx(
                org_id=seed["org_id"],
                account_id=primary.id,
                category_id=seed["cat_expense"],
                amount=Decimal("10.00"),
                type=TransactionType.EXPENSE,
                status=TransactionStatus.PENDING,
                date=BEFORE_PERIOD,
                settled_date=IN_PERIOD,
            ),
            _new_tx(
                org_id=seed["org_id"],
                account_id=primary.id,
                category_id=seed["cat_expense"],
                amount=Decimal("20.00"),
                type=TransactionType.EXPENSE,
                status=TransactionStatus.PENDING,
                date=IN_PERIOD,
                settled_date=None,
            ),
            _new_tx(
                org_id=seed["org_id"],
                account_id=primary.id,
                category_id=seed["cat_expense"],
                amount=Decimal("40.00"),
                type=TransactionType.EXPENSE,
                status=TransactionStatus.PENDING,
                date=IN_PERIOD,
                settled_date=AFTER_PERIOD,
            ),
        ]
    )
    await db_session.commit()

    result = await compute_account_balance_forecast(
        db_session, seed["org_id"], period_start=PERIOD_START
    )

    row = next(a for a in result["accounts"] if a["account_id"] == primary.id)
    # -10 (settled_date in-period) + -20 (no settled_date, date in-period) = -30
    assert row["pending_delta"] == "-30.00"
    assert row["expected_month_end_balance"] == "970.00"


# ---------- Test 8: manual adjustments do not affect pending delta ----------

async def test_manual_adjustments_excluded_from_pending_delta(
    db_session: AsyncSession,
):
    """Manual adjustments are settled-only today, but defensively excluded
    so a future change to allow pending manual adjustments doesn't
    silently start landing in the dashboard projection."""
    seed = await _seed(db_session)
    primary = seed["accounts"]["primary"]

    db_session.add(
        _new_tx(
            org_id=seed["org_id"],
            account_id=primary.id,
            category_id=seed["cat_expense"],
            amount=Decimal("999.00"),
            type=TransactionType.EXPENSE,
            status=TransactionStatus.PENDING,
            date=IN_PERIOD,
            is_manual_adjustment=True,
        )
    )
    await db_session.commit()

    result = await compute_account_balance_forecast(
        db_session, seed["org_id"], period_start=PERIOD_START
    )

    row = next(a for a in result["accounts"] if a["account_id"] == primary.id)
    assert row["pending_delta"] == "0.00"
    assert row["expected_month_end_balance"] == "1000.00"
