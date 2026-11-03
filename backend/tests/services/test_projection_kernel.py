"""Fences for ``projection_kernel.baseline`` (TBD-599).

The kernel is the one multi-month projection the dashboard forecast and Plans
share. The dashboard's own behaviour is pinned by
``test_account_balance_forecast_service.py``, unchanged; these tests pin what
is new: the differential against the dashboard over a LONGER horizon, the
point schedule, the per-currency series, ``exclude_recurring_ids`` and loans
across several months.
"""
import datetime
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization, Transaction
from app.models.base import Base
from app.models.billing import BillingPeriod
from app.models.category import CategoryType
from app.models.recurring import RecurringTransaction
from app.models.transaction import TransactionStatus, TransactionType
from app.services import projection_kernel
from app.services.account_balance_forecast_service import (
    compute_account_balance_forecast,
)
from app.services.loan_service import compute_pmt


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


# Calendar-aligned period: the dashboard's month-end IS a calendar month-end.
P_START = datetime.date(2026, 5, 1)
P_END = datetime.date(2026, 5, 31)
TODAY = datetime.date(2026, 5, 10)


async def _seed(db: AsyncSession) -> dict:
    """Two EUR accounts and one USD account, a generated PENDING recurring row
    (frontier already past it), an un-materialised USD series and a plain
    pending row. Balances are chosen so no currency could hide in another."""
    org = Organization(name="Kernel", billing_cycle_day=1)
    db.add(org)
    await db.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    cat = Category(org_id=org.id, name="Bills", slug="bills", type=CategoryType.EXPENSE)
    db.add_all([at, cat])
    await db.flush()

    eur_a = Account(org_id=org.id, name="EUR A", account_type_id=at.id,
                    balance=Decimal("1000.00"), currency="EUR", is_default=True)
    eur_b = Account(org_id=org.id, name="EUR B", account_type_id=at.id,
                    balance=Decimal("300.00"), currency="EUR", is_default=False)
    usd = Account(org_id=org.id, name="USD", account_type_id=at.id,
                  balance=Decimal("7000.00"), currency="USD", is_default=False)
    db.add_all([eur_a, eur_b, usd])
    db.add(BillingPeriod(org_id=org.id, start_date=P_START, end_date=P_END))
    await db.flush()

    # Rent: the May occurrence has been GENERATED as a pending row and the
    # frontier moved on to June (TBD-510: the generated row must still count).
    rent = RecurringTransaction(
        org_id=org.id, account_id=eur_a.id, category_id=cat.id,
        description="rent", amount=Decimal("400.00"), type="expense",
        frequency="monthly", next_due_date=datetime.date(2026, 6, 20),
        auto_settle=False, is_active=True,
    )
    # USD salary, never materialised.
    salary = RecurringTransaction(
        org_id=org.id, account_id=usd.id, category_id=cat.id,
        description="salary", amount=Decimal("2500.00"), type="income",
        frequency="monthly", next_due_date=datetime.date(2026, 5, 25),
        auto_settle=False, is_active=True,
    )
    db.add_all([rent, salary])
    await db.flush()

    def tx(account, amount, on, *, recurring_id=None):
        return Transaction(
            org_id=org.id, account_id=account.id, category_id=cat.id,
            amount=Decimal(amount), type=TransactionType.EXPENSE,
            status=TransactionStatus.PENDING, date=on, settled_date=None,
            description="x", is_imported=False, is_manual_adjustment=False,
            recurring_id=recurring_id,
        )

    db.add_all([
        tx(eur_a, "400.00", datetime.date(2026, 5, 20), recurring_id=rent.id),
        tx(eur_b, "50.00", datetime.date(2026, 5, 15)),
    ])
    await db.commit()
    return {"org_id": org.id, "eur_a": eur_a, "eur_b": eur_b, "usd": usd,
            "rent": rent, "salary": salary}


def _at(points, day):
    return dict(points)[day]


async def test_differential_month_end_equals_dashboard_per_account_and_currency(db_session):
    """DoD fence. With zero goals the kernel, run over a 4-month horizon,
    reads the dashboard's ``expected_month_end_balance`` at the period's
    month-end, per account AND per currency."""
    seed = await _seed(db_session)
    fc = await compute_account_balance_forecast(
        db_session, seed["org_id"], period_start=P_START, today=TODAY,
    )
    kb = await projection_kernel.baseline(
        db_session, seed["org_id"], today=TODAY,
        horizon_end=datetime.date(2026, 8, 31), window_start=P_START,
    )

    # Anti-vacuity: the generated pending rent row and the USD salary both
    # moved the number, so a kernel that dropped either would fail below.
    by_id = {a["account_id"]: a for a in fc["accounts"]}
    assert by_id[seed["eur_a"].id]["expected_month_end_balance"] == "600.00"
    assert by_id[seed["usd"].id]["expected_month_end_balance"] == "9500.00"

    kernel_rows = {a.account.id: a for a in kb.accounts}
    assert set(kernel_rows) == set(by_id)
    for account_id, row in by_id.items():
        month_end = _at(kernel_rows[account_id].points, P_END)
        assert str(month_end.quantize(Decimal("0.01"))) == row["expected_month_end_balance"]

    totals = {t["currency"]: t["expected_month_end_balance"] for t in fc["totals"]}
    assert set(kb.currencies) == set(totals) == {"EUR", "USD"}
    for currency, expected in totals.items():
        assert str(_at(kb.currencies[currency], P_END).quantize(Decimal("0.01"))) == expected

    # And the horizon really is longer: June's rent and salary land after May.
    assert _at(kernel_rows[seed["eur_a"].id].points, datetime.date(2026, 6, 30)) == Decimal("200.00")
    assert kernel_rows[seed["eur_a"].id].end_balance == Decimal("-600.00")


async def test_currency_series_never_sums_across_currencies(db_session):
    """Fence. Each currency series is the sum of ITS accounts only, at every
    point; the USD account never leaks into EUR or vice versa."""
    seed = await _seed(db_session)
    kb = await projection_kernel.baseline(
        db_session, seed["org_id"], today=TODAY, horizon_end=datetime.date(2026, 7, 31),
    )
    rows = {a.account.id: a for a in kb.accounts}
    eur = [rows[seed["eur_a"].id].points, rows[seed["eur_b"].id].points]
    expected_eur = [(d, a + b) for (d, a), (_, b) in zip(*eur)]
    assert kb.currencies["EUR"] == expected_eur
    assert kb.currencies["USD"] == rows[seed["usd"].id].points


async def test_exclude_recurring_ids_drops_frontier_and_generated_pending_rows(db_session):
    """Excluding rent removes its generated pending May row AND its June/July
    occurrences, and nothing else: the plain (non-recurring) pending row on
    EUR B survives the ``NOT IN`` (a bare ``NOT IN`` would drop NULLs)."""
    seed = await _seed(db_session)
    horizon = datetime.date(2026, 7, 31)
    kb = await projection_kernel.baseline(
        db_session, seed["org_id"], today=TODAY, horizon_end=horizon,
        window_start=P_START, exclude_recurring_ids=[seed["rent"].id],
    )
    rows = {a.account.id: a for a in kb.accounts}
    eur_a = rows[seed["eur_a"].id]
    assert eur_a.pending_delta == Decimal("0")
    assert eur_a.recurring_lines == []
    assert eur_a.end_balance == Decimal("1000.00")
    assert rows[seed["eur_b"].id].pending_delta == Decimal("-50.00")
    # The other series is untouched: three salaries (May, June, July).
    assert rows[seed["usd"].id].end_balance == Decimal("14500.00")

    # Control: without the exclusion rent costs 400 x 3.
    full = await projection_kernel.baseline(
        db_session, seed["org_id"], today=TODAY, horizon_end=horizon, window_start=P_START,
    )
    full_rows = {a.account.id: a for a in full.accounts}
    assert full_rows[seed["eur_a"].id].end_balance == Decimal("-200.00")


async def test_points_daily_for_24_months_then_month_ends(db_session):
    """Months 0-23 are daily, later months contribute one month-end point,
    and the last point is ``horizon_end`` even mid-month."""
    seed = await _seed(db_session)
    horizon = datetime.date(2029, 2, 10)
    kb = await projection_kernel.baseline(
        db_session, seed["org_id"], today=TODAY, horizon_end=horizon,
    )
    dates = [d for d, _ in kb.accounts[0].points]
    assert dates[0] == TODAY
    last_daily = datetime.date(2028, 4, 30)  # end of month 23 (May 2026 is month 0)
    i = dates.index(last_daily)
    assert dates[: i + 1] == [
        TODAY + datetime.timedelta(days=n) for n in range((last_daily - TODAY).days + 1)
    ]
    assert dates[i + 1:] == [
        datetime.date(2028, m, d) for m, d in
        ((5, 31), (6, 30), (7, 31), (8, 31), (9, 30), (10, 31), (11, 30), (12, 31))
    ] + [datetime.date(2029, 1, 31), horizon]
    # Monthly points are still end-of-day running totals: 33 salaries
    # (May 2026 .. Jan 2029) on the USD account.
    usd = next(a for a in kb.accounts if a.account.id == seed["usd"].id)
    assert usd.end_balance == Decimal("7000.00") + 33 * Decimal("2500.00")


async def test_loan_pays_once_per_month_across_the_horizon(db_session):
    """The dashboard's one-payment-per-period loan rule, generalised: a loan
    pays every month of a multi-month horizon, a month that already has a
    payment-in leg is skipped, and the final instalment caps at what is owed."""
    org = Organization(name="Loan", billing_cycle_day=1)
    db_session.add(org)
    await db_session.flush()
    checking = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    loan_type = AccountType(org_id=org.id, name="Loan", slug="loan", is_system=True)
    cat = Category(org_id=org.id, name="Transfer", slug="transfer",
                   type=CategoryType.BOTH, is_system=True)
    db_session.add_all([checking, loan_type, cat])
    await db_session.flush()
    source = Account(org_id=org.id, name="Src", account_type_id=checking.id,
                     balance=Decimal("5000.00"), currency="EUR", is_default=True)
    db_session.add(source)
    await db_session.flush()
    pmt = compute_pmt(Decimal("12000.00"), Decimal("6.00"), 60)
    owed = pmt * 2 + Decimal("10.00")  # two full payments and a 10.00 tail
    loan = Account(
        org_id=org.id, name="Loan", account_type_id=loan_type.id,
        balance=-owed, opening_balance=-owed, currency="EUR", is_default=False,
        payment_source_account_id=source.id, principal_amount=Decimal("12000.00"),
        interest_rate_apr=Decimal("6.00"), term_months=60,
        origination_date=datetime.date(2026, 4, 1),
        first_payment_date=datetime.date(2026, 5, 15),
    )
    db_session.add(loan)
    await db_session.flush()
    # A pending payment already booked for June: June is skipped.
    legs = [
        Transaction(org_id=org.id, account_id=acct.id, category_id=cat.id,
                    amount=pmt, type=kind, status=TransactionStatus.PENDING,
                    date=datetime.date(2026, 6, 15), description="loan",
                    is_imported=False, is_manual_adjustment=False)
        for acct, kind in ((source, TransactionType.EXPENSE), (loan, TransactionType.INCOME))
    ]
    db_session.add_all(legs)
    await db_session.flush()
    legs[0].linked_transaction_id = legs[1].id
    legs[1].linked_transaction_id = legs[0].id
    await db_session.commit()

    kb = await projection_kernel.baseline(
        db_session, org.id, today=P_START, horizon_end=datetime.date(2026, 9, 30),
    )
    row = next(a for a in kb.accounts if a.account.id == loan.id)
    assert row.loan_payments == [
        (datetime.date(2026, 5, 15), pmt),
        (datetime.date(2026, 7, 15), pmt),
        (datetime.date(2026, 8, 15), Decimal("10.00")),
    ]
    # Conserving: the source drops by exactly what the loan gains.
    src = next(a for a in kb.accounts if a.account.id == source.id)
    assert src.end_balance - src.balance == -(row.end_balance - row.balance)
