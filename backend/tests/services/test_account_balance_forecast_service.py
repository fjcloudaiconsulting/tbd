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
from sqlalchemy import event, select
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
from app.models.transaction import TransactionStatus, TransactionType
from app.schemas.forecast import AccountBalanceForecastResponse
from app.services import cc_statement_service as css
from app.services.account_balance_forecast_service import (
    compute_account_balance_forecast,
)
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
    """Reviewer finding on the Task 3 refactor: ``load_cc_ledgers``'s
    ``up_to`` bound was previously mandatory and the forecast site passed
    ``p_end`` (the horizon's period_end). That is only safe if
    ``payment_date >= close_date`` for every due cycle -- an invariant that
    does NOT hold. With ``payment_day < close_day`` and
    ``payment_day_relative_month == 0`` (same-month payment),
    ``_resolve_payment_date`` can yield ``payment_date < close_date``. If
    the horizon's ``p_end`` then falls between them,
    ``due_cycles_in_horizon`` still includes the cycle (its
    ``payment_date <= p_end``) even though ``close_date > p_end`` --  so
    bounding the ledger fetch at ``p_end`` drops rows in
    ``(p_end, close_date]`` that ``balance_at_close(close_date)`` needs,
    silently under-counting outstanding (and, transitively, the Task 9
    close-day alert, which shares this module). Fixed: ``load_cc_ledgers``'s
    ``up_to`` is optional and the forecast call site now passes none
    (restoring the pre-refactor unbounded fetch). On the pre-fix code this
    test is RED (``cc_payments == []``); on the fix it is GREEN.

    Cycle here: close_day=25, payment_day=10, payment_day_relative_month=0
    -> for the cycle closing 2026-05-25, payment_date resolves to
    2026-05-10 (BEFORE close). The charge lands on 2026-05-20 -- after
    payment_date, after p_end, but at-or-before close_date.
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
    assert cc_row["cc_payments"] == [{"amount": "400.00", "date": "2026-05-10"}]

    # Safe call site cross-check: statement_outstanding (up_to=close_date)
    # must also include the post-p_end, pre-close charge -- it always has
    # (this call site was never the bug), but pins the shared amount so the
    # forecast and the Task 9 alert can never diverge for this cycle.
    owed = await css.statement_outstanding(
        db_session, org_id=org_id, account=cc, close_date=close_date
    )
    assert owed == Decimal("400.00")
