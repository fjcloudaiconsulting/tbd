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
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import (
    Account,
    AccountType,
    Category,
    ImportBatch,
    ImportBatchStatus,
    ImportSourceFormat,
    Organization,
    Transaction,
    User,
)
from app.models.account import PaymentStrategy
from app.models.base import Base
from app.models.billing import BillingPeriod
from app.models.category import CategoryType
from app.models.cc_cycle_payment import CcCyclePayment
from app.models.recurring import RecurringTransaction
from app.models.transaction import TransactionStatus, TransactionType
from app.schemas.forecast import AccountBalanceForecastResponse
from app.services import cc_statement_service as css
from app.services.account_balance_forecast_service import (
    compute_account_balance_forecast,
)
from app.services.loan_service import compute_pmt
from app.services.transaction_filters import balance_contribution_filter


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


# ---------- Slice 3: CC projected-payment synthesis ----------

async def _seed_cc(
    db: AsyncSession,
    *,
    strategy=PaymentStrategy.FULL_BALANCE,
    fixed_payment_amount=None,
    cc_currency="EUR",
    source_currency="EUR",
    close_day=25,
    opening_balance=Decimal("0.00"),
):
    """Seed a checking source + a credit_card paid from it. Returns the base
    _seed() dict plus 'cc', 'source', 'cc_type_id'."""
    seed = await _seed(db)
    org_id = seed["org_id"]
    source = seed["accounts"]["primary"]
    if source_currency != source.currency:
        source.currency = source_currency
    cc_type = AccountType(org_id=org_id, name="Credit Card", slug="credit_card", is_system=True)
    db.add(cc_type)
    await db.flush()
    cc = Account(
        org_id=org_id, name="Visa", account_type_id=cc_type.id,
        balance=Decimal("0.00"), currency=cc_currency, is_default=False,
        close_day=close_day, payment_day=1, payment_day_relative_month=1,
        payment_source_account_id=source.id, payment_strategy=strategy,
        fixed_payment_amount=fixed_payment_amount, opening_balance=opening_balance,
    )
    db.add(cc)
    await db.flush()
    seed["cc"] = cc
    seed["source"] = source
    seed["cc_type_id"] = cc_type.id
    return seed


def _charge(seed, cc, *, amount, on, settled=True):
    """A settled CC expense (lowers the CC balance)."""
    return _new_tx(
        org_id=seed["org_id"], account_id=cc.id, category_id=seed["cat_expense"],
        amount=Decimal(amount), type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED if settled else TransactionStatus.PENDING,
        date=on, settled_date=on if settled else None,
    )


async def test_cc_synth_grace_period_uses_balance_as_of_close(db_session: AsyncSession):
    """(h)+(a) close in the past, due in horizon: outflow == owed AS OF CLOSE."""
    seed = await _seed_cc(db_session)
    cc, source = seed["cc"], seed["source"]
    db_session.add_all([
        _charge(seed, cc, amount="500.00", on=datetime.date(2026, 4, 10)),
        _charge(seed, cc, amount="700.00", on=datetime.date(2026, 5, 3)),
    ])
    cc.balance = Decimal("-1200.00")
    await db_session.commit()
    result = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    by_id = {a["account_id"]: a for a in result["accounts"]}
    cc_row, src_row = by_id[cc.id], by_id[source.id]
    assert cc_row["cc_payments"] == [{"amount": "500.00", "date": "2026-05-01"}]
    assert Decimal(cc_row["expected_month_end_balance"]) == Decimal(cc_row["balance"]) + Decimal("500.00")
    assert Decimal(src_row["expected_month_end_balance"]) == (
        Decimal(src_row["balance"]) + Decimal(src_row["pending_delta"]) - Decimal("500.00")
    )


async def test_cc_synth_conservation_same_currency(db_session: AsyncSession):
    """(b) totals unchanged; Σ per-account expected == Σ(balance+pending)."""
    seed = await _seed_cc(db_session)
    cc = seed["cc"]
    db_session.add(_charge(seed, cc, amount="300.00", on=datetime.date(2026, 4, 10)))
    cc.balance = Decimal("-300.00")
    await db_session.commit()
    result = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    eur = next(t for t in result["totals"] if t["currency"] == "EUR")
    assert eur["expected_month_end_balance"] == str(
        (Decimal(eur["balance"]) + Decimal(eur["pending_delta"])).quantize(Decimal("0.01")))
    eur_rows = [a for a in result["accounts"] if a["currency"] == "EUR"]
    assert sum(Decimal(a["expected_month_end_balance"]) for a in eur_rows) == sum(
        Decimal(a["balance"]) + Decimal(a["pending_delta"]) for a in eur_rows)


async def test_cc_synth_null_source_value_parity(db_session: AsyncSession):
    """(e) NULL source -> no synth; money fields match pre-Slice-3; cc_payments empty."""
    seed = await _seed_cc(db_session)
    cc = seed["cc"]
    cc.payment_source_account_id = None
    db_session.add(_charge(seed, cc, amount="800.00", on=datetime.date(2026, 4, 10)))
    cc.balance = Decimal("-800.00")
    await db_session.commit()
    result = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    for a in result["accounts"]:
        assert a["cc_payments"] == []
        assert a["expected_month_end_balance"] == str(
            (Decimal(a["balance"]) + Decimal(a["pending_delta"])).quantize(Decimal("0.01")))


async def test_cc_synth_cross_currency_source_no_op(db_session: AsyncSession):
    """(f) source currency != CC currency -> no synthesis."""
    seed = await _seed_cc(db_session, cc_currency="EUR", source_currency="USD")
    cc = seed["cc"]
    db_session.add(_charge(seed, cc, amount="400.00", on=datetime.date(2026, 4, 10)))
    cc.balance = Decimal("-400.00")
    await db_session.commit()
    result = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    cc_row = next(a for a in result["accounts"] if a["account_id"] == cc.id)
    assert cc_row["cc_payments"] == []


async def test_cc_synth_card_in_credit_no_outflow(db_session: AsyncSession):
    """(g) nothing owed -> no outflow."""
    seed = await _seed_cc(db_session)
    cc = seed["cc"]
    cc.balance = Decimal("120.00")
    await db_session.commit()
    result = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    cc_row = next(a for a in result["accounts"] if a["account_id"] == cc.id)
    assert cc_row["cc_payments"] == []


async def test_cc_synth_fixed_amount_clamped_to_owed(db_session: AsyncSession):
    """(c)+(k) fixed_amount literal, clamped so it never pays into credit."""
    seed = await _seed_cc(db_session, strategy=PaymentStrategy.FIXED_AMOUNT,
                          fixed_payment_amount=Decimal("500.00"))
    cc = seed["cc"]
    db_session.add(_charge(seed, cc, amount="300.00", on=datetime.date(2026, 4, 10)))
    cc.balance = Decimal("-300.00")
    await db_session.commit()
    result = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    cc_row = next(a for a in result["accounts"] if a["account_id"] == cc.id)
    assert cc_row["cc_payments"] == [{"amount": "300.00", "date": "2026-05-01"}]


async def test_cc_synth_override_applies_to_full_balance(db_session: AsyncSession):
    """F2: a per-cycle override is honored on a full_balance card."""
    seed = await _seed_cc(db_session)  # default strategy = FULL_BALANCE
    cc = seed["cc"]
    db_session.add(_charge(seed, cc, amount="900.00", on=datetime.date(2026, 4, 10)))
    cc.balance = Decimal("-900.00")
    await db_session.commit()
    r1 = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    assert next(a for a in r1["accounts"] if a["account_id"] == cc.id)["cc_payments"] == [
        {"amount": "900.00", "date": "2026-05-01"}]
    db_session.add(CcCyclePayment(account_id=cc.id, period_anchor_year=2026,
                                  period_anchor_month=4, amount=Decimal("75.00")))
    await db_session.commit()
    r2 = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    assert next(a for a in r2["accounts"] if a["account_id"] == cc.id)["cc_payments"] == [
        {"amount": "75.00", "date": "2026-05-01"}]


async def test_cc_synth_real_payment_nets_once(db_session: AsyncSession):
    """(j) a real CC payment-in credit in (close, due] nets P_k."""
    seed = await _seed_cc(db_session)
    cc, source = seed["cc"], seed["source"]
    db_session.add(_charge(seed, cc, amount="1000.00", on=datetime.date(2026, 4, 10)))
    src_leg = _new_tx(org_id=seed["org_id"], account_id=source.id, category_id=seed["cat_transfer"],
                      amount=Decimal("300.00"), type=TransactionType.EXPENSE,
                      status=TransactionStatus.SETTLED, date=datetime.date(2026, 4, 28),
                      settled_date=datetime.date(2026, 4, 28))
    cc_leg = _new_tx(org_id=seed["org_id"], account_id=cc.id, category_id=seed["cat_transfer"],
                     amount=Decimal("300.00"), type=TransactionType.INCOME,
                     status=TransactionStatus.SETTLED, date=datetime.date(2026, 4, 28),
                     settled_date=datetime.date(2026, 4, 28))
    db_session.add_all([src_leg, cc_leg])
    await db_session.flush()
    src_leg.linked_transaction_id = cc_leg.id
    cc_leg.linked_transaction_id = src_leg.id
    cc.balance = Decimal("-700.00")
    source.balance = source.balance - Decimal("300.00")
    await db_session.commit()
    result = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    cc_row = next(a for a in result["accounts"] if a["account_id"] == cc.id)
    assert cc_row["cc_payments"] == [{"amount": "700.00", "date": "2026-05-01"}]


async def test_cc_synth_two_due_dates_s_prev(db_session: AsyncSession):
    """(i) a two-month horizon bills carried debt once."""
    seed = await _seed_cc(db_session)
    cc = seed["cc"]
    db_session.add(_charge(seed, cc, amount="1000.00", on=datetime.date(2026, 3, 10)))
    cc.balance = Decimal("-1000.00")
    seed["period"].end_date = datetime.date(2026, 6, 30)
    await db_session.commit()
    result = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    cc_row = next(a for a in result["accounts"] if a["account_id"] == cc.id)
    assert cc_row["cc_payments"] == [{"amount": "1000.00", "date": "2026-05-01"}]


# ---------- Slice 3 fix: reconcile-matched reverted duplicates excluded ----------


async def _make_import_batch(db: AsyncSession, seed: dict, account_id: int) -> ImportBatch:
    """A minimal ``ImportBatch`` + owning user, enough to attach a
    reconcile-matched imported duplicate row to via ``import_batch_id``."""
    user = User(
        username="importer",
        email="importer@example.com",
        password_hash="x",
        org_id=seed["org_id"],
        is_superadmin=False,
    )
    db.add(user)
    await db.flush()
    batch = ImportBatch(
        org_id=seed["org_id"],
        account_id=account_id,
        source_format=ImportSourceFormat.CSV,
        file_name="dup.csv",
        created_by_user_id=user.id,
        status=ImportBatchStatus.OPEN,
    )
    db.add(batch)
    await db.flush()
    return batch


async def test_cc_synth_excludes_reconcile_matched_reverted_duplicate_ledger(
    db_session: AsyncSession,
):
    """Money-moving regression: a reconcile-MATCHED imported duplicate of a
    settled CC charge has its balance contribution REVERTED at match time
    (``reconciliation_service._apply_balance_for_transition``), but
    ``non_reverted_transaction_filter()`` only excludes skipped/rejected
    rows -- so a naive ledger query double-counts the canonical charge via
    the duplicate, doubling B_k's owed amount and the projected CC payment.

    The duplicate here: status=settled, reconciliation_state=matched,
    import_batch_id set, linked_transaction_id pointing at the canonical
    charge -- and its amount is NOT reflected in ``cc.balance`` (reverted).
    Fixed code must reconstruct B_k from -500 (the canonical charge only),
    yielding an outflow of 500.00, not 1000.00.
    """
    seed = await _seed_cc(db_session)
    cc, source = seed["cc"], seed["source"]

    canonical = _charge(seed, cc, amount="500.00", on=datetime.date(2026, 4, 10))
    db_session.add(canonical)
    await db_session.flush()

    batch = await _make_import_batch(db_session, seed, cc.id)
    duplicate = _new_tx(
        org_id=seed["org_id"],
        account_id=cc.id,
        category_id=seed["cat_expense"],
        amount=Decimal("500.00"),
        type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED,
        date=datetime.date(2026, 4, 10),
        settled_date=datetime.date(2026, 4, 10),
        is_imported=True,
        import_batch_id=batch.id,
        reconciliation_state="matched",
        linked_transaction_id=canonical.id,
    )
    db_session.add(duplicate)
    # Balance reflects ONLY the canonical charge -- the duplicate's
    # contribution was reverted when it was matched.
    cc.balance = Decimal("-500.00")
    await db_session.commit()

    result = await compute_account_balance_forecast(
        db_session, seed["org_id"], period_start=PERIOD_START
    )
    cc_row = next(a for a in result["accounts"] if a["account_id"] == cc.id)
    assert cc_row["cc_payments"] == [{"amount": "500.00", "date": "2026-05-01"}]


async def test_cc_synth_excludes_reconcile_matched_reverted_duplicate_credit(
    db_session: AsyncSession,
):
    """Symmetric credits-side (P_k) coverage. Built on the same fixture as
    ``test_cc_synth_real_payment_nets_once``: a real CC payment-in transfer
    leg pair (300.00) nets against the 1000.00 charge for an expected
    outflow of 700.00. A reconcile-MATCHED imported duplicate of the
    payment-in leg (import_batch_id set, linked_transaction_id pointing at
    the canonical transfer leg, reverted balance contribution) must not be
    counted a second time in P_k -- without the fix it is, halving the
    outflow to 400.00.
    """
    seed = await _seed_cc(db_session)
    cc, source = seed["cc"], seed["source"]
    db_session.add(_charge(seed, cc, amount="1000.00", on=datetime.date(2026, 4, 10)))
    src_leg = _new_tx(
        org_id=seed["org_id"], account_id=source.id, category_id=seed["cat_transfer"],
        amount=Decimal("300.00"), type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED, date=datetime.date(2026, 4, 28),
        settled_date=datetime.date(2026, 4, 28),
    )
    cc_leg = _new_tx(
        org_id=seed["org_id"], account_id=cc.id, category_id=seed["cat_transfer"],
        amount=Decimal("300.00"), type=TransactionType.INCOME,
        status=TransactionStatus.SETTLED, date=datetime.date(2026, 4, 28),
        settled_date=datetime.date(2026, 4, 28),
    )
    db_session.add_all([src_leg, cc_leg])
    await db_session.flush()
    src_leg.linked_transaction_id = cc_leg.id
    cc_leg.linked_transaction_id = src_leg.id

    batch = await _make_import_batch(db_session, seed, cc.id)
    dup_credit = _new_tx(
        org_id=seed["org_id"],
        account_id=cc.id,
        category_id=seed["cat_transfer"],
        amount=Decimal("300.00"),
        type=TransactionType.INCOME,
        status=TransactionStatus.SETTLED,
        date=datetime.date(2026, 4, 28),
        settled_date=datetime.date(2026, 4, 28),
        is_imported=True,
        import_batch_id=batch.id,
        reconciliation_state="matched",
        linked_transaction_id=cc_leg.id,
    )
    db_session.add(dup_credit)

    cc.balance = Decimal("-700.00")
    source.balance = source.balance - Decimal("300.00")
    await db_session.commit()

    result = await compute_account_balance_forecast(
        db_session, seed["org_id"], period_start=PERIOD_START
    )
    cc_row = next(a for a in result["accounts"] if a["account_id"] == cc.id)
    assert cc_row["cc_payments"] == [{"amount": "700.00", "date": "2026-05-01"}]


# ---------- Slice 3 architect correction: reciprocal-link discriminator ----------
#
# The flat-column predicate (import_batch_id IS NULL OR linked_transaction_id
# IS NULL) over-excludes: a genuine transfer leg that happens to be
# import-paired is byte-identical, in every flat column, to a reconcile-
# MATCHED duplicate (both can carry import_batch_id set + linked_transaction_id
# set + reconciliation_state='accepted'). The corrected discriminator is
# partner-link RECIPROCITY: ``_link_pair`` (real transfers, incl. import
# pairing) links BIDIRECTIONALLY; ``_apply_match`` (reconcile match) links
# ONE-WAY onto the duplicate only. See transaction_filters.balance_contribution_filter.


async def test_cc_synth_keeps_import_paired_transfer_leg_no_phantom_repayment(
    db_session: AsyncSession,
):
    """THE BUG: a real, import-paired payment-in transfer leg (bidirectional
    link + import_batch_id set on the CC leg) must still be KEPT in the B_k
    ledger. Under the old flat-column filter it was wrongly dropped because
    import_batch_id and linked_transaction_id are both set -- indistinguishable
    from a reconcile-matched duplicate by flat columns alone. Dropping it
    understates the ledger's reconstructed balance (still looks owed) and
    the forecast synthesizes a phantom re-payment of debt that is already
    paid off. On the OLD filter this assertion fails (RED); the fix makes
    it pass (GREEN)."""
    seed = await _seed_cc(db_session)
    cc, source = seed["cc"], seed["source"]
    db_session.add(_charge(seed, cc, amount="1000.00", on=datetime.date(2026, 4, 10)))

    src_leg = _new_tx(
        org_id=seed["org_id"], account_id=source.id, category_id=seed["cat_transfer"],
        amount=Decimal("1000.00"), type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED, date=datetime.date(2026, 4, 20),
        settled_date=datetime.date(2026, 4, 20),
    )
    cc_leg = _new_tx(
        org_id=seed["org_id"], account_id=cc.id, category_id=seed["cat_transfer"],
        amount=Decimal("1000.00"), type=TransactionType.INCOME,
        status=TransactionStatus.SETTLED, date=datetime.date(2026, 4, 20),
        settled_date=datetime.date(2026, 4, 20),
    )
    db_session.add_all([src_leg, cc_leg])
    await db_session.flush()

    # Import-paired real transfer: BIDIRECTIONAL link (what _link_pair
    # writes, including at import time), import_batch_id set on the CC
    # leg (this leg arrived via a bank import), state accepted.
    batch = await _make_import_batch(db_session, seed, cc.id)
    src_leg.linked_transaction_id = cc_leg.id
    cc_leg.linked_transaction_id = src_leg.id
    cc_leg.import_batch_id = batch.id
    cc_leg.reconciliation_state = "accepted"

    cc.balance = Decimal("0.00")  # fully paid down by the real transfer
    source.balance = source.balance - Decimal("1000.00")
    await db_session.commit()

    result = await compute_account_balance_forecast(
        db_session, seed["org_id"], period_start=PERIOD_START
    )
    cc_row = next(a for a in result["accounts"] if a["account_id"] == cc.id)
    assert cc_row["cc_payments"] == []  # no phantom re-payment of already-paid debt


async def test_cc_synth_drops_one_way_matched_duplicate_state_matched(
    db_session: AsyncSession,
):
    """Explicit reciprocity-discriminator coverage: a reconcile-MATCHED
    duplicate carries a ONE-WAY link (only the duplicate points at the
    canonical row; the canonical row is not linked back) -- exactly what
    ``_apply_match`` writes. Confirms the corrected filter still drops
    these (terminal state 'matched')."""
    seed = await _seed_cc(db_session)
    cc = seed["cc"]
    canonical = _charge(seed, cc, amount="500.00", on=datetime.date(2026, 4, 10))
    db_session.add(canonical)
    await db_session.flush()

    batch = await _make_import_batch(db_session, seed, cc.id)
    duplicate = _new_tx(
        org_id=seed["org_id"], account_id=cc.id, category_id=seed["cat_expense"],
        amount=Decimal("500.00"), type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED, date=datetime.date(2026, 4, 10),
        settled_date=datetime.date(2026, 4, 10), is_imported=True,
        import_batch_id=batch.id, reconciliation_state="matched",
        linked_transaction_id=canonical.id,  # one-way; canonical stays unlinked
    )
    db_session.add(duplicate)
    cc.balance = Decimal("-500.00")  # duplicate's contribution reverted at match time
    await db_session.commit()

    result = await compute_account_balance_forecast(
        db_session, seed["org_id"], period_start=PERIOD_START
    )
    cc_row = next(a for a in result["accounts"] if a["account_id"] == cc.id)
    assert cc_row["cc_payments"] == [{"amount": "500.00", "date": "2026-05-01"}]


async def test_cc_synth_drops_one_way_matched_duplicate_state_accepted(
    db_session: AsyncSession,
):
    """Same shape as the 'matched' case, but terminal state 'accepted' --
    refutes the hypothesis that the corrected filter discriminates on
    ``state != 'matched'`` rather than on link reciprocity. The duplicate
    must still be dropped."""
    seed = await _seed_cc(db_session)
    cc = seed["cc"]
    canonical = _charge(seed, cc, amount="500.00", on=datetime.date(2026, 4, 10))
    db_session.add(canonical)
    await db_session.flush()

    batch = await _make_import_batch(db_session, seed, cc.id)
    duplicate = _new_tx(
        org_id=seed["org_id"], account_id=cc.id, category_id=seed["cat_expense"],
        amount=Decimal("500.00"), type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED, date=datetime.date(2026, 4, 10),
        settled_date=datetime.date(2026, 4, 10), is_imported=True,
        import_batch_id=batch.id, reconciliation_state="accepted",  # NOT "matched"
        linked_transaction_id=canonical.id,  # one-way; canonical stays unlinked
    )
    db_session.add(duplicate)
    cc.balance = Decimal("-500.00")
    await db_session.commit()

    result = await compute_account_balance_forecast(
        db_session, seed["org_id"], period_start=PERIOD_START
    )
    cc_row = next(a for a in result["accounts"] if a["account_id"] == cc.id)
    assert cc_row["cc_payments"] == [{"amount": "500.00", "date": "2026-05-01"}]


async def test_balance_contribution_filter_invariant_matches_account_balance(
    db_session: AsyncSession,
):
    """Filter-level invariant, queried directly (no forecast synthesis
    involved): Σ signed(settled rows passing balance_contribution_filter())
    == account.balance - account.opening_balance, across every row shape --
    unlinked reportable (keep), manual adjustment (keep), an import-paired
    BIDIRECTIONAL transfer leg (keep), a ONE-WAY matched-reverted duplicate
    (drop), skipped (drop), rejected (drop)."""
    seed = await _seed_cc(db_session, opening_balance=Decimal("1000.00"))
    cc, source = seed["cc"], seed["source"]
    batch = await _make_import_batch(db_session, seed, cc.id)

    plain = _charge(seed, cc, amount="200.00", on=datetime.date(2026, 4, 5))

    adj = _new_tx(
        org_id=seed["org_id"], account_id=cc.id, category_id=seed["cat_expense"],
        amount=Decimal("50.00"), type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED, date=datetime.date(2026, 4, 6),
        settled_date=datetime.date(2026, 4, 6), is_manual_adjustment=True,
    )

    # Import-paired BIDIRECTIONAL transfer leg -- KEEP despite carrying
    # import_batch_id, the exact shape the bug wrongly dropped.
    src_leg = _new_tx(
        org_id=seed["org_id"], account_id=source.id, category_id=seed["cat_transfer"],
        amount=Decimal("300.00"), type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED, date=datetime.date(2026, 4, 7),
        settled_date=datetime.date(2026, 4, 7),
    )
    cc_leg = _new_tx(
        org_id=seed["org_id"], account_id=cc.id, category_id=seed["cat_transfer"],
        amount=Decimal("300.00"), type=TransactionType.INCOME,
        status=TransactionStatus.SETTLED, date=datetime.date(2026, 4, 7),
        settled_date=datetime.date(2026, 4, 7), is_imported=True,
        import_batch_id=batch.id, reconciliation_state="accepted",
    )

    db_session.add_all([plain, adj, src_leg, cc_leg])
    await db_session.flush()
    src_leg.linked_transaction_id = cc_leg.id
    cc_leg.linked_transaction_id = src_leg.id

    # ONE-WAY matched-reverted duplicate of `plain` -- DROP.
    dup = _new_tx(
        org_id=seed["org_id"], account_id=cc.id, category_id=seed["cat_expense"],
        amount=Decimal("77.00"), type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED, date=datetime.date(2026, 4, 8),
        settled_date=datetime.date(2026, 4, 8), is_imported=True,
        import_batch_id=batch.id, reconciliation_state="matched",
        linked_transaction_id=plain.id,  # one-way; plain stays unlinked
    )

    # SKIPPED -- DROP (reverted at state transition).
    skipped = _new_tx(
        org_id=seed["org_id"], account_id=cc.id, category_id=seed["cat_expense"],
        amount=Decimal("33.00"), type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED, date=datetime.date(2026, 4, 9),
        settled_date=datetime.date(2026, 4, 9), reconciliation_state="skipped",
    )

    # REJECTED -- DROP (reverted at state transition).
    rejected = _new_tx(
        org_id=seed["org_id"], account_id=cc.id, category_id=seed["cat_expense"],
        amount=Decimal("22.00"), type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED, date=datetime.date(2026, 4, 10),
        settled_date=datetime.date(2026, 4, 10), reconciliation_state="rejected",
    )

    db_session.add_all([dup, skipped, rejected])

    # Balance reflects ONLY the kept contributions: opening 1000.00
    # - 200.00 (plain) - 50.00 (adj) + 300.00 (cc_leg income) = 1050.00.
    # If `dup` (-77), `skipped` (-33), or `rejected` (-22) were wrongly
    # kept, or `cc_leg` (+300) were wrongly dropped, this invariant breaks.
    cc.balance = Decimal("1050.00")
    await db_session.commit()

    rows = (
        await db_session.execute(
            select(Transaction.type, Transaction.amount).where(
                Transaction.account_id == cc.id, balance_contribution_filter()
            )
        )
    ).all()
    total = Decimal("0")
    for tx_type, amount in rows:
        signed = Decimal(str(amount))
        total += signed if tx_type == TransactionType.INCOME else -signed

    assert cc.balance - cc.opening_balance == total


async def test_cc_credits_query_excludes_one_way_matched_duplicate_payment_in(
    db_session: AsyncSession,
):
    """Credits (P_k) symmetry: a reconcile-MATCHED (ONE-WAY linked) imported
    duplicate of a real CC payment-in leg must not double-net P_k -- only
    the reciprocal/canonical payment nets once. Without the fix (or if the
    reciprocity check were dropped from the credits side) this would net
    twice, halving the projected outflow."""
    seed = await _seed_cc(db_session)
    cc, source = seed["cc"], seed["source"]
    db_session.add(_charge(seed, cc, amount="1000.00", on=datetime.date(2026, 4, 10)))

    src_leg = _new_tx(
        org_id=seed["org_id"], account_id=source.id, category_id=seed["cat_transfer"],
        amount=Decimal("300.00"), type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED, date=datetime.date(2026, 4, 28),
        settled_date=datetime.date(2026, 4, 28),
    )
    cc_leg = _new_tx(
        org_id=seed["org_id"], account_id=cc.id, category_id=seed["cat_transfer"],
        amount=Decimal("300.00"), type=TransactionType.INCOME,
        status=TransactionStatus.SETTLED, date=datetime.date(2026, 4, 28),
        settled_date=datetime.date(2026, 4, 28),
    )
    db_session.add_all([src_leg, cc_leg])
    await db_session.flush()
    src_leg.linked_transaction_id = cc_leg.id
    cc_leg.linked_transaction_id = src_leg.id

    batch = await _make_import_batch(db_session, seed, cc.id)
    dup_credit = _new_tx(
        org_id=seed["org_id"], account_id=cc.id, category_id=seed["cat_transfer"],
        amount=Decimal("300.00"), type=TransactionType.INCOME,
        status=TransactionStatus.SETTLED, date=datetime.date(2026, 4, 28),
        settled_date=datetime.date(2026, 4, 28), is_imported=True,
        import_batch_id=batch.id, reconciliation_state="matched",
        linked_transaction_id=cc_leg.id,  # one-way; cc_leg stays unlinked to dup
    )
    db_session.add(dup_credit)

    cc.balance = Decimal("-700.00")  # 1000 charge - 300 real payment only
    source.balance = source.balance - Decimal("300.00")
    await db_session.commit()

    result = await compute_account_balance_forecast(
        db_session, seed["org_id"], period_start=PERIOD_START
    )
    cc_row = next(a for a in result["accounts"] if a["account_id"] == cc.id)
    # 700, not 400 -- the duplicate credit must not net a second time.
    assert cc_row["cc_payments"] == [{"amount": "700.00", "date": "2026-05-01"}]


# ---------- Slice 3, Task 3: response-model provenance round-trip ----------


async def test_account_balance_forecast_response_preserves_cc_payments(
    db_session: AsyncSession,
):
    """The /api/v1/forecast/account-balances response_model must not
    silently strip the synthesized cc_payments provenance off the
    per-account row (Credit Card Model V1, Slice 3, Task 3)."""
    seed = await _seed_cc(db_session)
    cc, source = seed["cc"], seed["source"]
    db_session.add(_charge(seed, cc, amount="500.00", on=datetime.date(2026, 4, 10)))
    cc.balance = Decimal("-500.00")
    await db_session.commit()

    result = await compute_account_balance_forecast(
        db_session, seed["org_id"], period_start=PERIOD_START
    )

    response = AccountBalanceForecastResponse(**result)

    cc_row = next(a for a in response.accounts if a.account_id == cc.id)
    assert len(cc_row.cc_payments) == 1
    payment = cc_row.cc_payments[0]
    assert payment.amount == Decimal("500.00")
    assert payment.date == datetime.date(2026, 5, 1)

    source_row = next(a for a in response.accounts if a.account_id == source.id)
    assert source_row.cc_payments == []


# ---------- Review finding: payment_date < close_date must not truncate ledger ----------


async def test_cc_synth_payment_before_close_does_not_drop_ledger_row(
    db_session: AsyncSession,
):
    """A payment-before-close cycle is SKIPPED by the forecast (belt), and
    ``load_cc_ledgers`` still fetches unbounded for the alert path.

    Two things are pinned here:

    1. **Belt (cc_forecast_service).** ``close_day=25, payment_day=10,
       payment_day_relative_month=0`` resolves the 2026-05-25 cycle's
       payment to 2026-05-10 -- BEFORE close. Projecting a payment on
       05-10 for a charge made 05-20 is nonsense (paying before the charge
       exists), and its empty credit-attribution window overstates the
       outflow, so ``synthesize_account_cc_payments`` now skips the
       degenerate cycle: ``cc_payments == []``. This config is also
       forbidden at create/PUT by the same-month payment-before-close
       validation guard; the row is constructed directly in the DB here to
       exercise the forecast's defensive skip.

    2. **Unbounded ledger fetch (the original reviewer finding).**
       ``load_cc_ledgers``'s ``up_to`` was once mandatory and the forecast
       passed ``p_end``, which drops rows in ``(p_end, close_date]`` that
       ``balance_at_close(close_date)`` needs. That invariant is now
       pinned on the live path via the ``statement_outstanding``
       cross-check below (``up_to=close_date`` must include the post-p_end,
       pre-close 05-20 charge -> owed 400), which the Task 9 close-day
       alert shares.

    Cycle here: close_day=25, payment_day=10, payment_day_relative_month=0
    -> the cycle closing 2026-05-25 has payment_date 2026-05-10 (BEFORE
    close). The charge lands on 2026-05-20 -- after payment_date, after
    p_end, but at-or-before close_date.
    """
    seed = await _seed(db_session)
    org_id = seed["org_id"]
    source = seed["accounts"]["primary"]

    cc_type = AccountType(org_id=org_id, name="Credit Card", slug="credit_card", is_system=True)
    db_session.add(cc_type)
    await db_session.flush()

    cc = Account(
        org_id=org_id, name="Visa", account_type_id=cc_type.id,
        balance=Decimal("-400.00"), currency="EUR", is_default=False,
        close_day=25, payment_day=10, payment_day_relative_month=0,
        payment_source_account_id=source.id, opening_balance=Decimal("0.00"),
    )
    db_session.add(cc)
    await db_session.flush()

    charge_date = datetime.date(2026, 5, 20)
    close_date = datetime.date(2026, 5, 25)
    db_session.add(
        _new_tx(
            org_id=org_id, account_id=cc.id, category_id=seed["cat_expense"],
            amount=Decimal("400.00"), type=TransactionType.EXPENSE,
            status=TransactionStatus.SETTLED, date=charge_date, settled_date=charge_date,
        )
    )

    # Horizon ends BEFORE the close date but AFTER the payment date --
    # exactly the window where due_cycles_in_horizon includes the cycle
    # (payment_date=2026-05-10 <= p_end) while close_date=2026-05-25 > p_end.
    seed["period"].end_date = datetime.date(2026, 5, 15)
    await db_session.commit()

    result = await compute_account_balance_forecast(
        db_session, org_id, period_start=PERIOD_START
    )
    cc_row = next(a for a in result["accounts"] if a["account_id"] == cc.id)
    # Belt: the degenerate payment-before-close cycle is skipped, not
    # projected on 05-10 (which would pre-date the 05-20 charge).
    assert cc_row["cc_payments"] == []

    # Safe call site cross-check: statement_outstanding (up_to=close_date)
    # must also include the post-p_end, pre-close charge -- it always has
    # (this call site was never the bug), but pins the shared amount so the
    # forecast and the Task 9 alert can never diverge for this cycle.
    owed = await css.statement_outstanding(
        db_session, org_id=org_id, account=cc, close_date=close_date
    )
    assert owed == Decimal("400.00")


# ---------- Slice 2: Loan projected-payment synthesis ----------

_LOAN_PRINCIPAL = Decimal("12000.00")
_LOAN_APR = Decimal("6.00")
_LOAN_TERM = 60
_LOAN_PMT = compute_pmt(_LOAN_PRINCIPAL, _LOAN_APR, _LOAN_TERM)  # ~232.00


async def _seed_loan(
    db: AsyncSession,
    *,
    loan_currency="EUR",
    source_currency="EUR",
    balance=Decimal("-10000.00"),
    first_payment_date=IN_PERIOD,
    term_months=_LOAN_TERM,
):
    """Seed a checking source + a loan paid from it. Adds 'loan', 'source'."""
    seed = await _seed(db)
    org_id = seed["org_id"]
    source = seed["accounts"]["primary"]
    if source_currency != source.currency:
        source.currency = source_currency
    loan_type = AccountType(org_id=org_id, name="Loan", slug="loan", is_system=True)
    db.add(loan_type)
    await db.flush()
    loan = Account(
        org_id=org_id, name="Car Loan", account_type_id=loan_type.id,
        balance=balance, currency=loan_currency, is_default=False,
        payment_source_account_id=source.id, opening_balance=balance,
        principal_amount=_LOAN_PRINCIPAL, interest_rate_apr=_LOAN_APR,
        term_months=term_months, origination_date=datetime.date(2026, 5, 1),
        first_payment_date=first_payment_date,
    )
    db.add(loan)
    await db.flush()
    seed["loan"] = loan
    seed["source"] = source
    return seed


async def _add_loan_payment(db, seed, loan, source, *, amount, on=IN_PERIOD, settled=True):
    """A linked source->loan transfer pair (the 'already paid this period' leg)."""
    st = TransactionStatus.SETTLED if settled else TransactionStatus.PENDING
    exp = _new_tx(
        org_id=seed["org_id"], account_id=source.id, category_id=seed["cat_transfer"],
        amount=Decimal(amount), type=TransactionType.EXPENSE, status=st,
        date=on, settled_date=on if settled else None,
    )
    inc = _new_tx(
        org_id=seed["org_id"], account_id=loan.id, category_id=seed["cat_transfer"],
        amount=Decimal(amount), type=TransactionType.INCOME, status=st,
        date=on, settled_date=on if settled else None,
    )
    db.add_all([exp, inc])
    await db.flush()
    exp.linked_transaction_id = inc.id
    inc.linked_transaction_id = exp.id


async def test_loan_synth_projects_one_payment(db_session: AsyncSession):
    seed = await _seed_loan(db_session)
    loan, source = seed["loan"], seed["source"]
    await db_session.commit()
    result = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    by_id = {a["account_id"]: a for a in result["accounts"]}
    loan_row, src_row = by_id[loan.id], by_id[source.id]
    assert loan_row["loan_payments"] == [{"amount": str(_LOAN_PMT), "date": "2026-05-15"}]
    # loan moves toward zero by PMT; source drops by PMT
    assert Decimal(loan_row["expected_month_end_balance"]) == Decimal(loan_row["balance"]) + _LOAN_PMT
    assert Decimal(src_row["expected_month_end_balance"]) == (
        Decimal(src_row["balance"]) + Decimal(src_row["pending_delta"]) - _LOAN_PMT
    )


async def test_loan_synth_conservation_totals_unchanged(db_session: AsyncSession):
    seed = await _seed_loan(db_session)
    await db_session.commit()
    result = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    # Σ synth == 0: per-account expected sums to Σ(balance+pending) per currency.
    for tot in result["totals"]:
        ccy = tot["currency"]
        acct_sum = sum(
            Decimal(a["expected_month_end_balance"])
            for a in result["accounts"] if a["currency"] == ccy
        )
        assert acct_sum == Decimal(tot["expected_month_end_balance"])
        assert Decimal(tot["expected_month_end_balance"]) == (
            Decimal(tot["balance"]) + Decimal(tot["pending_delta"])
        )


async def test_loan_synth_paid_off_noop(db_session: AsyncSession):
    seed = await _seed_loan(db_session, balance=Decimal("0.00"))
    loan = seed["loan"]
    await db_session.commit()
    result = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    loan_row = {a["account_id"]: a for a in result["accounts"]}[loan.id]
    assert loan_row["loan_payments"] == []


async def test_loan_synth_final_installment_caps_at_outstanding(db_session: AsyncSession):
    # owed 100 < PMT -> applied = 100 (never drive loan positive).
    seed = await _seed_loan(db_session, balance=Decimal("-100.00"))
    loan, source = seed["loan"], seed["source"]
    await db_session.commit()
    result = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    by_id = {a["account_id"]: a for a in result["accounts"]}
    assert by_id[loan.id]["loan_payments"] == [{"amount": "100.00", "date": "2026-05-15"}]
    assert Decimal(by_id[loan.id]["expected_month_end_balance"]) == Decimal("0.00")


async def test_loan_synth_skips_when_settled_payment_in_period(db_session: AsyncSession):
    seed = await _seed_loan(db_session)
    loan, source = seed["loan"], seed["source"]
    await _add_loan_payment(db_session, seed, loan, source, amount="232.00", settled=True)
    await db_session.commit()
    result = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    loan_row = {a["account_id"]: a for a in result["accounts"]}[loan.id]
    # already paid (settled linked leg in period) -> no phantom projection
    assert loan_row["loan_payments"] == []


async def test_loan_synth_skips_when_pending_payment_in_period(db_session: AsyncSession):
    # The load-bearing pending case: a pending linked payment is already in
    # pending_delta; synthesis MUST skip or it double-counts.
    seed = await _seed_loan(db_session)
    loan, source = seed["loan"], seed["source"]
    await _add_loan_payment(db_session, seed, loan, source, amount="232.00", settled=False)
    await db_session.commit()
    result = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    by_id = {a["account_id"]: a for a in result["accounts"]}
    loan_row, src_row = by_id[loan.id], by_id[source.id]
    assert loan_row["loan_payments"] == []
    # pending_delta already moved both legs exactly once (no double count)
    assert Decimal(loan_row["pending_delta"]) == Decimal("232.00")
    assert Decimal(loan_row["expected_month_end_balance"]) == (
        Decimal(loan_row["balance"]) + Decimal("232.00")
    )
    assert Decimal(src_row["expected_month_end_balance"]) == (
        Decimal(src_row["balance"]) + Decimal(src_row["pending_delta"])
    )


async def test_loan_synth_fx_mismatch_skips(db_session: AsyncSession):
    seed = await _seed_loan(db_session, source_currency="USD")
    loan = seed["loan"]
    await db_session.commit()
    result = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    loan_row = {a["account_id"]: a for a in result["accounts"]}[loan.id]
    assert loan_row["loan_payments"] == []


async def test_loan_synth_inactive_source_noop(db_session: AsyncSession):
    seed = await _seed_loan(db_session)
    loan, source = seed["loan"], seed["source"]
    source.is_active = False
    await db_session.commit()
    result = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    loan_row = {a["account_id"]: a for a in result["accounts"]}[loan.id]
    assert loan_row["loan_payments"] == []


async def test_loan_synth_past_term_not_projected(db_session: AsyncSession):
    # 12-month loan first paid a year ago -> last payment 2026-04-15 (< window).
    seed = await _seed_loan(
        db_session, first_payment_date=datetime.date(2025, 5, 15), term_months=12
    )
    loan = seed["loan"]
    await db_session.commit()
    result = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    loan_row = {a["account_id"]: a for a in result["accounts"]}[loan.id]
    assert loan_row["loan_payments"] == []


async def test_loan_and_cc_share_source_accumulates_both(db_session: AsyncSession):
    # A source funding BOTH a CC and a loan must keep both synth deltas.
    seed = await _seed_loan(db_session)
    loan, source = seed["loan"], seed["source"]
    cc_type = AccountType(org_id=seed["org_id"], name="Credit Card", slug="credit_card", is_system=True)
    db_session.add(cc_type)
    await db_session.flush()
    cc = Account(
        org_id=seed["org_id"], name="Visa", account_type_id=cc_type.id,
        balance=Decimal("-300.00"), currency="EUR", is_default=False,
        close_day=25, payment_day=1, payment_day_relative_month=1,
        payment_source_account_id=source.id, opening_balance=Decimal("0.00"),
    )
    db_session.add(cc)
    await db_session.flush()
    db_session.add(_charge(seed, cc, amount="300.00", on=datetime.date(2026, 4, 10)))
    await db_session.commit()
    result = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    by_id = {a["account_id"]: a for a in result["accounts"]}
    src_row = by_id[source.id]
    # source drops by BOTH the loan payment and the CC payment (300)
    assert Decimal(src_row["expected_month_end_balance"]) == (
        Decimal(src_row["balance"]) + Decimal(src_row["pending_delta"]) - _LOAN_PMT - Decimal("300.00")
    )
    assert by_id[loan.id]["loan_payments"] == [{"amount": str(_LOAN_PMT), "date": "2026-05-15"}]
    assert by_id[cc.id]["cc_payments"] == [{"amount": "300.00", "date": "2026-05-01"}]


async def test_loan_synth_linked_expense_leg_does_not_suppress(db_session: AsyncSession):
    # A linked EXPENSE leg on the loan (e.g. a disbursement, which increases
    # owed) must NOT trip the already-paid skip -- only INCOME payment-in legs
    # do. Guards the `type == INCOME` predicate.
    seed = await _seed_loan(db_session)
    loan, source = seed["loan"], seed["source"]
    exp = _new_tx(
        org_id=seed["org_id"], account_id=loan.id, category_id=seed["cat_transfer"],
        amount=Decimal("500.00"), type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED, date=IN_PERIOD, settled_date=IN_PERIOD,
    )
    inc = _new_tx(
        org_id=seed["org_id"], account_id=source.id, category_id=seed["cat_transfer"],
        amount=Decimal("500.00"), type=TransactionType.INCOME,
        status=TransactionStatus.SETTLED, date=IN_PERIOD, settled_date=IN_PERIOD,
    )
    db_session.add_all([exp, inc])
    await db_session.flush()
    exp.linked_transaction_id = inc.id
    inc.linked_transaction_id = exp.id
    await db_session.commit()
    result = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    loan_row = {a["account_id"]: a for a in result["accounts"]}[loan.id]
    assert loan_row["loan_payments"] == [{"amount": str(_LOAN_PMT), "date": "2026-05-15"}]


async def test_loan_synth_unlinked_income_leg_does_not_suppress(db_session: AsyncSession):
    # An UNLINKED income leg on the loan (not a transfer payment) must NOT
    # suppress. Guards the `linked_transaction_id IS NOT NULL` predicate.
    seed = await _seed_loan(db_session)
    loan = seed["loan"]
    db_session.add(_new_tx(
        org_id=seed["org_id"], account_id=loan.id, category_id=seed["cat_income"],
        amount=Decimal("400.00"), type=TransactionType.INCOME,
        status=TransactionStatus.SETTLED, date=IN_PERIOD, settled_date=IN_PERIOD,
    ))
    await db_session.commit()
    result = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    loan_row = {a["account_id"]: a for a in result["accounts"]}[loan.id]
    assert loan_row["loan_payments"] == [{"amount": str(_LOAN_PMT), "date": "2026-05-15"}]


async def test_loan_synth_out_of_period_payment_does_not_suppress(db_session: AsyncSession):
    # A linked payment settled in a PRIOR period must NOT suppress the current
    # period's projection. Guards the eff-in-[p_start,p_end] bound.
    seed = await _seed_loan(db_session)
    loan, source = seed["loan"], seed["source"]
    await _add_loan_payment(
        db_session, seed, loan, source, amount="232.00", on=BEFORE_PERIOD, settled=True
    )
    await db_session.commit()
    result = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    loan_row = {a["account_id"]: a for a in result["accounts"]}[loan.id]
    assert loan_row["loan_payments"] == [{"amount": str(_LOAN_PMT), "date": "2026-05-15"}]


# ═════════════════════════════════════════════════════════════════════════════
# TBD-198 — Low balance day warning
#
# The month-end number is now the LAST POINT of a daily series rather than a
# separate expression, and `risk_days` is a READ of that series. Every fence
# below was proven RED against a NAMED wrong implementation; the mutant is
# stated in each docstring.
# ═════════════════════════════════════════════════════════════════════════════

# The clock, injected. Anchored ON `PERIOD_START` so `walk_start == p_start`
# and the series spans the whole period: with a mid-period clock the first
# days collapse into the day-0 seed and the fixtures below would be pinning a
# shorter series than they read.
TODAY_IN_PERIOD = PERIOD_START


def _daily(row) -> list[tuple[str, str]]:
    return [(p["date"], p["balance"]) for p in row["daily_balances"]]


async def test_f1_daily_series_last_point_is_the_month_end_total(
    db_session: AsyncSession,
):
    """F1. `daily_balances[-1].balance == expected_month_end_balance`, per account.

    Mutant killed: `expected = balance + delta + synth` (the pre-TBD-198
    expression) computed BESIDE the walk instead of taken FROM it. That mutant
    silently drops the recurring projection, so the card's headline and the
    series it draws disagree on the same row.

    NON-VACUITY: the fixture carries ONE OF EACH delta source, and each is
    pinned by its own assertion before the conservation loop runs --
      * a PENDING row               -> `pending_delta == -200.00`
      * an UN-MATERIALISED recurring occurrence
      * a synthesized CC payment    -> `cc_payments == [500.00 on 2026-05-01]`
    A pending-only fixture passes this test even with the recurring port
    omitted entirely, which is why the -50.00 assertion (1000 - 200 - 350 - 500)
    is load-bearing: it is the ONLY assertion that fails when the recurring
    projection is missing.
    """
    seed = await _seed_cc(db_session)
    cc, source = seed["cc"], seed["source"]
    # (1) CC source: one settled charge before the 04-25 close -> a 500.00
    #     payment projected on 2026-05-01.
    db_session.add(_charge(seed, cc, amount="500.00", on=datetime.date(2026, 4, 10)))
    cc.balance = Decimal("-500.00")
    # (2) pending source.
    db_session.add(_new_tx(
        org_id=seed["org_id"], account_id=source.id, category_id=seed["cat_expense"],
        amount=Decimal("200.00"), type=TransactionType.EXPENSE,
        status=TransactionStatus.PENDING, date=datetime.date(2026, 5, 20),
    ))
    # (3) recurring source, NEVER materialised (no row carries this
    #     recurring_id) -- the occurrence generation has not created yet.
    db_session.add(RecurringTransaction(
        org_id=seed["org_id"], account_id=source.id, category_id=seed["cat_expense"],
        description="rent", amount=Decimal("350.00"), type="expense",
        frequency="monthly", next_due_date=datetime.date(2026, 5, 10),
        auto_settle=False, is_active=True,
    ))
    await db_session.commit()

    result = await compute_account_balance_forecast(
        db_session, seed["org_id"], period_start=PERIOD_START, today=TODAY_IN_PERIOD
    )
    by_id = {a["account_id"]: a for a in result["accounts"]}
    src_row, cc_row = by_id[source.id], by_id[cc.id]

    # Precondition: nothing materialised this template.
    assert (await db_session.execute(
        select(func.count()).select_from(Transaction)
        .where(Transaction.recurring_id.is_not(None))
    )).scalar() == 0

    # All three sources present and each individually visible.
    assert src_row["pending_delta"] == "-200.00"
    assert cc_row["cc_payments"] == [{"amount": "500.00", "date": "2026-05-01"}]
    # 1000 - 200 (pending) - 350 (recurring) - 500 (CC payment) = -50.00.
    # This is the assertion the recurring port owns.
    assert src_row["expected_month_end_balance"] == "-50.00"

    # THE INVARIANT, for every account.
    for row in result["accounts"]:
        assert row["daily_balances"], row["account_name"]
        assert row["daily_balances"][-1]["date"] == "2026-05-31"
        assert (
            row["daily_balances"][-1]["balance"]
            == row["expected_month_end_balance"]
        ), row["account_name"]


async def test_f2_threshold_is_strict_zero_is_not_overdrawn(
    db_session: AsyncSession,
):
    """F2. Two ADJACENT days, one landing on exactly 0.00 and one on -0.01.

    Mutant killed: `balance <= LOW_BALANCE_THRESHOLD` for `<` in `_risk_runs`.

    NON-VACUITY: the amounts are chosen so the balance lands EXACTLY on 0.00
    (1000.00 - 1000.00), never on 0.004 -- a fixture that only gets near zero
    cannot tell the two operators apart. Injecting `<=` flips EXACTLY ONE of
    the two assertions below: `len(risk) == 1` still holds (05-10 simply joins
    the same contiguous run), and `from == 2026-05-11` becomes
    `from == 2026-05-10`. Both assertions are therefore required; either alone
    is survivable.
    """
    seed = await _seed(db_session)
    primary = seed["accounts"]["primary"]
    db_session.add_all([
        _new_tx(
            org_id=seed["org_id"], account_id=primary.id,
            category_id=seed["cat_expense"], amount=Decimal("1000.00"),
            type=TransactionType.EXPENSE, status=TransactionStatus.PENDING,
            date=datetime.date(2026, 5, 10),
        ),
        _new_tx(
            org_id=seed["org_id"], account_id=primary.id,
            category_id=seed["cat_expense"], amount=Decimal("0.01"),
            type=TransactionType.EXPENSE, status=TransactionStatus.PENDING,
            date=datetime.date(2026, 5, 11),
        ),
    ])
    await db_session.commit()

    result = await compute_account_balance_forecast(
        db_session, seed["org_id"], period_start=PERIOD_START, today=TODAY_IN_PERIOD
    )
    row = next(a for a in result["accounts"] if a["account_id"] == primary.id)

    # The geometry the fence depends on: exactly 0.00, then exactly -0.01.
    daily = dict(_daily(row))
    assert daily["2026-05-10"] == "0.00"
    assert daily["2026-05-11"] == "-0.01"

    risk = row["risk_days"]
    assert len(risk) == 1
    assert risk[0]["from"] == "2026-05-11"       # NOT 05-10: 0.00 is not overdrawn
    assert risk[0]["through"] == "2026-05-31"
    assert risk[0]["lowest_balance"] == "-0.01"


async def test_f3_risk_is_per_account_and_never_crosses_currencies(
    db_session: AsyncSession,
):
    """F3. A EUR account at -100 is flagged; a USD account at +5000 is not, and
    `totals[]` carries no risk field at all.

    Mutant killed: evaluating the risk series on the per-currency `totals`
    rollup (or on any cross-account sum) instead of per account.

    NON-VACUITY: the USD balance is 5000, deliberately large enough that a
    NAIVE cross-currency sum stays POSITIVE (5000 - 100 = 4900) and therefore
    flags nothing. With a small USD balance the naive sum is also negative,
    the broken implementation flags EUR too, and the test passes against it.
    """
    seed = await _seed(db_session, second_currency=True)
    primary = seed["accounts"]["primary"]
    usd = seed["accounts"]["usd"]
    usd.balance = Decimal("5000.00")
    db_session.add(_new_tx(
        org_id=seed["org_id"], account_id=primary.id,
        category_id=seed["cat_expense"], amount=Decimal("1100.00"),
        type=TransactionType.EXPENSE, status=TransactionStatus.PENDING,
        date=datetime.date(2026, 5, 12),
    ))
    await db_session.commit()

    result = await compute_account_balance_forecast(
        db_session, seed["org_id"], period_start=PERIOD_START, today=TODAY_IN_PERIOD
    )
    by_id = {a["account_id"]: a for a in result["accounts"]}

    eur_row = by_id[primary.id]
    assert eur_row["currency"] == "EUR"
    assert eur_row["expected_month_end_balance"] == "-100.00"
    assert len(eur_row["risk_days"]) == 1
    assert eur_row["risk_days"][0]["from"] == "2026-05-12"
    assert eur_row["risk_days"][0]["lowest_balance"] == "-100.00"

    usd_row = by_id[usd.id]
    assert usd_row["currency"] == "USD"
    assert usd_row["expected_month_end_balance"] == "5000.00"
    assert usd_row["risk_days"] == []

    # The one place a cross-currency sum could enter. It must stay closed.
    for total in result["totals"]:
        assert "risk_days" not in total
        assert "daily_balances" not in total


async def test_f5_future_dated_settled_row_does_not_pre_spend_day_zero(
    db_session: AsyncSession,
):
    """F5. A SETTLED expense dated `window_end` is already inside
    `accounts.balance` today -- `accounts.balance` is date-agnostic and nothing
    validates a transaction's date against the clock. Day 0 must therefore ADD
    it back, and the walk must lay it down again on its own date.

    Mutant killed: seeding day 0 with `account.balance` verbatim and never
    querying future-dated settled rows -- which is what the code did before
    TBD-198 and what a builder writes by default.

    NON-VACUITY: BOTH ends are asserted, and they are not interchangeable.
    Under that mutant the FINAL value is 1000.00 either way, so the last-day
    assertion passes; only the day-0 assertion (1300.00 vs 1000.00)
    discriminates.
    """
    seed = await _seed(db_session)
    primary = seed["accounts"]["primary"]
    # `primary.balance` stays 1000.00: the row below is ALREADY counted in it.
    db_session.add(_new_tx(
        org_id=seed["org_id"], account_id=primary.id,
        category_id=seed["cat_expense"], amount=Decimal("300.00"),
        type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
        date=PERIOD_END, settled_date=PERIOD_END,
    ))
    await db_session.commit()

    result = await compute_account_balance_forecast(
        db_session, seed["org_id"], period_start=PERIOD_START, today=TODAY_IN_PERIOD
    )
    row = next(a for a in result["accounts"] if a["account_id"] == primary.id)
    daily = _daily(row)

    assert row["balance"] == "1000.00"
    assert daily[0] == ("2026-05-01", "1300.00")      # un-spent: THE fence
    assert daily[-1] == ("2026-05-31", "1000.00")     # re-spent on its own day
    assert row["expected_month_end_balance"] == "1000.00"
    # The day before it lands is still the un-spent figure.
    assert dict(daily)["2026-05-30"] == "1300.00"


async def test_f6_credit_cards_and_loans_are_never_flagged(
    db_session: AsyncSession,
):
    """F6. A CC at -1,200 and a loan at -40,000 produce ZERO risk days: a
    negative balance is the NORMAL state of a liability, so an implementation
    without the deny-list warns on them every single day.

    Mutant killed: dropping the `LIABILITY_SLUGS` guard in `_risk_runs`.

    ⚠ THE CLOCK IS BEFORE `PERIOD_START` HERE, DELIBERATELY, and this test is
    vacuous without it. With `today == PERIOD_START` a liability that is
    already below zero on day 0 sits inside a run that STARTS on day 0, and R3
    ("strictly future") suppresses that run on its own -- so the deny-list can
    be deleted and the test still passes. Measured, not theorised: the first
    draft of this fence was GREEN against its own mutant for exactly that
    reason. Moving the clock one week earlier makes every run start strictly
    after `today`, which leaves the deny-list as the ONLY thing that can
    suppress them.

    NON-VACUITY, three ways:
      * a CONTROL account -- an ordinary `checking` row at -50.00 under the
        identical clock and window -- IS flagged, proving R3 is not doing the
        suppressing;
      * the CC carries BOTH `close_day` AND `payment_source_account_id`, so CC
        synthesis genuinely runs (asserted via a non-empty `cc_payments`);
        without those the account is skipped for an UNRELATED reason;
      * both liability day-0 balances are asserted below zero, so the guard has
        real work to do.
    """
    seed = await _seed_cc(db_session)
    cc, source = seed["cc"], seed["source"]
    db_session.add(_charge(seed, cc, amount="500.00", on=datetime.date(2026, 4, 10)))
    cc.balance = Decimal("-1200.00")
    # The control: a NON-liability account, below zero on the very same days.
    control = seed["accounts"]["secondary"]
    control.balance = Decimal("-50.00")
    before_period = datetime.date(2026, 4, 24)

    loan_type = AccountType(
        org_id=seed["org_id"], name="Loan", slug="loan", is_system=True
    )
    db_session.add(loan_type)
    await db_session.flush()
    loan = Account(
        org_id=seed["org_id"], name="Mortgage", account_type_id=loan_type.id,
        balance=Decimal("-40000.00"), currency="EUR", is_default=False,
        payment_source_account_id=source.id, opening_balance=Decimal("-40000.00"),
        principal_amount=_LOAN_PRINCIPAL, interest_rate_apr=_LOAN_APR,
        term_months=_LOAN_TERM, origination_date=datetime.date(2026, 5, 1),
        first_payment_date=IN_PERIOD,
    )
    db_session.add(loan)
    await db_session.commit()

    result = await compute_account_balance_forecast(
        db_session, seed["org_id"], period_start=PERIOD_START, today=before_period
    )
    by_id = {a["account_id"]: a for a in result["accounts"]}
    cc_row, loan_row = by_id[cc.id], by_id[loan.id]
    control_row = by_id[control.id]

    # Non-vacuity: synthesis ran, and both accounts really are below zero.
    assert cc_row["cc_payments"] == [{"amount": "500.00", "date": "2026-05-01"}]
    assert loan_row["loan_payments"] == [{"amount": str(_LOAN_PMT), "date": "2026-05-15"}]
    assert Decimal(cc_row["daily_balances"][0]["balance"]) < 0
    assert Decimal(loan_row["daily_balances"][0]["balance"]) < 0

    # Non-vacuity: the CONTROL, on the same clock and the same window, IS
    # flagged from the first day of the series. Anything that suppresses the
    # two rows below would have suppressed this one too.
    assert control_row["account_type_slug"] == "checking"
    assert len(control_row["risk_days"]) == 1
    assert control_row["risk_days"][0]["from"] == "2026-05-01"

    assert cc_row["risk_days"] == []
    assert loan_row["risk_days"] == []


async def test_f7_runs_not_days_two_separate_dips(db_session: AsyncSession):
    """F7 (R2). Each contiguous below-zero interval is ONE entry.

    Mutants killed:
      * one entry per below-zero DAY (this fixture would report 14);
      * reporting only the GLOBAL MINIMUM (would report 1).

    NON-VACUITY: the fixture dips, RECOVERS, and dips AGAIN, so the correct
    answer is TWO. A single-run fixture passes against the global-minimum
    implementation and proves nothing. The trough of run 1 (-300 on 05-11) is
    deliberately LOWER than run 2's (-50), so an implementation that keeps only
    the deepest run also fails.
    """
    seed = await _seed(db_session)
    primary = seed["accounts"]["primary"]
    org_id = seed["org_id"]

    def _p(amount, on, kind=TransactionType.EXPENSE):
        return _new_tx(
            org_id=org_id, account_id=primary.id,
            category_id=seed["cat_expense"] if kind == TransactionType.EXPENSE
            else seed["cat_income"],
            amount=Decimal(amount), type=kind,
            status=TransactionStatus.PENDING, date=on,
        )

    db_session.add_all([
        _p("1200.00", datetime.date(2026, 5, 10)),                        # -> -200
        _p("100.00", datetime.date(2026, 5, 11)),                         # -> -300
        _p("500.00", datetime.date(2026, 5, 12), TransactionType.INCOME),  # -> +200
        _p("250.00", datetime.date(2026, 5, 20)),                         # -> -50
    ])
    await db_session.commit()

    result = await compute_account_balance_forecast(
        db_session, org_id, period_start=PERIOD_START, today=TODAY_IN_PERIOD
    )
    row = next(a for a in result["accounts"] if a["account_id"] == primary.id)

    # The recovery is real, not an artefact of the assertions below.
    assert dict(_daily(row))["2026-05-12"] == "200.00"

    assert row["risk_days"] == [
        {
            "from": "2026-05-10",
            "through": "2026-05-11",
            "lowest_balance": "-300.00",
            "lowest_on": "2026-05-11",
        },
        {
            "from": "2026-05-20",
            "through": "2026-05-31",
            "lowest_balance": "-50.00",
            "lowest_on": "2026-05-20",
        },
    ]


async def test_f7b_already_negative_account_is_re_warned_not_warned(
    db_session: AsyncSession,
):
    """F7b (R3 + R4). An account below zero TODAY is not warned about today --
    that is current state and the Balance column already shows it, exactly.
    Only a SUBSEQUENT, genuinely new crossing is reported.

    Mutant killed: emitting every run regardless of its start date (i.e.
    dropping the `r["from"] > today` filter). That mutant reports TWO runs
    here, the first of which restates the balance the user is already looking
    at.

    NON-VACUITY: day 0 is asserted to be BELOW zero, so the suppressed run
    genuinely exists; and the account RECOVERS before dipping again, so there
    is a second run for the rule to let through. Without the recovery the
    single run is suppressed and `[] == []` proves nothing.
    """
    seed = await _seed(db_session)
    primary = seed["accounts"]["primary"]
    primary.balance = Decimal("-100.00")
    db_session.add_all([
        _new_tx(
            org_id=seed["org_id"], account_id=primary.id,
            category_id=seed["cat_income"], amount=Decimal("500.00"),
            type=TransactionType.INCOME, status=TransactionStatus.PENDING,
            date=datetime.date(2026, 5, 8),
        ),
        _new_tx(
            org_id=seed["org_id"], account_id=primary.id,
            category_id=seed["cat_expense"], amount=Decimal("600.00"),
            type=TransactionType.EXPENSE, status=TransactionStatus.PENDING,
            date=datetime.date(2026, 5, 20),
        ),
    ])
    await db_session.commit()

    result = await compute_account_balance_forecast(
        db_session, seed["org_id"], period_start=PERIOD_START, today=TODAY_IN_PERIOD
    )
    row = next(a for a in result["accounts"] if a["account_id"] == primary.id)
    daily = dict(_daily(row))

    # The suppressed run is real: below zero today, recovering on the 8th.
    assert daily["2026-05-01"] == "-100.00"
    assert daily["2026-05-07"] == "-100.00"
    assert daily["2026-05-08"] == "400.00"

    assert len(row["risk_days"]) == 1
    assert row["risk_days"][0]["from"] == "2026-05-20"
    assert row["risk_days"][0]["lowest_balance"] == "-200.00"


async def test_f_wire_risk_day_key_is_from_after_response_model_round_trip(
    db_session: AsyncSession,
):
    """The wire contract, at the API boundary rather than in the service dict.

    `from` is a Python keyword, so `RiskDayRun` spells the field `from_date`
    with an alias. FastAPI serialises response models with `by_alias=True`, so
    the JSON key the frontend reads must come back out as `from`.

    Mutant killed: declaring the field as plain `from_date` with no alias --
    the service dict still says `from`, pydantic silently drops the unknown
    key, and the widget's dated sub-line renders `undefined` with every
    backend test still green.
    """
    seed = await _seed(db_session)
    primary = seed["accounts"]["primary"]
    db_session.add(_new_tx(
        org_id=seed["org_id"], account_id=primary.id,
        category_id=seed["cat_expense"], amount=Decimal("1500.00"),
        type=TransactionType.EXPENSE, status=TransactionStatus.PENDING,
        date=datetime.date(2026, 5, 12),
    ))
    await db_session.commit()

    result = await compute_account_balance_forecast(
        db_session, seed["org_id"], period_start=PERIOD_START, today=TODAY_IN_PERIOD
    )
    response = AccountBalanceForecastResponse(**result)
    row = next(a for a in response.accounts if a.account_id == primary.id)

    assert len(row.risk_days) == 1
    assert row.risk_days[0].from_date == datetime.date(2026, 5, 12)

    wire = response.model_dump(mode="json", by_alias=True)
    wire_row = next(a for a in wire["accounts"] if a["account_id"] == primary.id)
    assert wire_row["risk_days"][0]["from"] == "2026-05-12"
    assert "from_date" not in wire_row["risk_days"][0]

    # And the invariant survives the round trip.
    assert (
        row.daily_balances[-1].balance == row.expected_month_end_balance
    )
