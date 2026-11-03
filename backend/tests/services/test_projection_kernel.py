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
from dateutil.relativedelta import relativedelta
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization, Transaction
from app.models.account import PaymentStrategy
from app.models.base import Base
from app.models.billing import BillingPeriod
from app.models.category import CategoryType
from app.models.recurring import RecurringTransaction
from app.models.transaction import TransactionStatus, TransactionType
from app.services import cc_forecast_service, projection_kernel
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

    db.add_all([
        _tx(org.id, eur_a, cat, "400.00", datetime.date(2026, 5, 20), recurring_id=rent.id),
        _tx(org.id, eur_b, cat, "50.00", datetime.date(2026, 5, 15)),
        # SETTLED and dated after the period: already inside EUR B's stored
        # 300.00, and the dashboard keeps it there for its month-end.
        _tx(org.id, eur_b, cat, "100.00", datetime.date(2026, 6, 10), settled=True),
    ])
    await db.commit()
    return {"org_id": org.id, "eur_a": eur_a, "eur_b": eur_b, "usd": usd,
            "rent": rent, "salary": salary}


def _tx(org_id, account, cat, amount, on, *, recurring_id=None, settled=False,
        kind=TransactionType.EXPENSE):
    return Transaction(
        org_id=org_id, account_id=account.id, category_id=cat.id,
        amount=Decimal(amount), type=kind,
        status=TransactionStatus.SETTLED if settled else TransactionStatus.PENDING,
        date=on, settled_date=on if settled else None,
        description="x", is_imported=False, is_manual_adjustment=False,
        recurring_id=recurring_id,
    )


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
    assert by_id[seed["eur_b"].id]["expected_month_end_balance"] == "250.00"

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


async def _org_with_source(db):
    org = Organization(name="Org", billing_cycle_day=1)
    db.add(org)
    await db.flush()
    checking = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    cat = Category(org_id=org.id, name="Transfer", slug="transfer",
                   type=CategoryType.BOTH, is_system=True)
    db.add_all([checking, cat])
    await db.flush()
    source = Account(org_id=org.id, name="Src", account_type_id=checking.id,
                     balance=Decimal("50000.00"), currency="EUR", is_default=True)
    db.add(source)
    await db.flush()
    return org, cat, source


async def _loan(db, org, source, *, owed, principal, apr, term, first_payment):
    loan_type = AccountType(org_id=org.id, name="Loan", slug="loan", is_system=True)
    db.add(loan_type)
    await db.flush()
    loan = Account(
        org_id=org.id, name="Loan", account_type_id=loan_type.id,
        balance=-owed, opening_balance=-owed, currency="EUR", is_default=False,
        payment_source_account_id=source.id, principal_amount=principal,
        interest_rate_apr=apr, term_months=term,
        origination_date=first_payment - relativedelta(months=1),
        first_payment_date=first_payment,
    )
    db.add(loan)
    await db.flush()
    return loan


async def _card(db, org, source, *, owed, fixed=None, charged_on=datetime.date(2026, 4, 10)):
    """A card closing on the 25th, paid on the 1st of the next month, with
    ``owed`` charged before the April close."""
    cc_type = AccountType(org_id=org.id, name="Card", slug="credit_card", is_system=True)
    db.add(cc_type)
    await db.flush()
    cc = Account(
        org_id=org.id, name="Visa", account_type_id=cc_type.id,
        balance=-owed, opening_balance=Decimal("0.00"), currency="EUR",
        is_default=False, close_day=25, payment_day=1, payment_day_relative_month=1,
        payment_source_account_id=source.id,
        payment_strategy=PaymentStrategy.FIXED_AMOUNT if fixed else PaymentStrategy.FULL_BALANCE,
        fixed_payment_amount=fixed,
    )
    db.add(cc)
    await db.flush()
    return cc


async def test_loan_pays_once_per_month_across_the_horizon(db_session):
    """The dashboard's one-payment-per-period loan rule, generalised: a loan
    pays every month of a multi-month horizon, a month that already has a
    payment-in leg is skipped and counted, and the loan never goes past zero."""
    org, cat, source = await _org_with_source(db_session)
    pmt = compute_pmt(Decimal("12000.00"), Decimal("6.00"), 60)
    owed = pmt * 2 + Decimal("10.00")  # two full payments and a 10.00 tail
    loan = await _loan(db_session, org, source, owed=owed, principal=Decimal("12000.00"),
                       apr=Decimal("6.00"), term=60, first_payment=datetime.date(2026, 5, 15))
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
    # May pays in full (period 0 is the dashboard's, interest-blind). June is
    # booked, so skipped, but accrues a month of interest. July starts from
    # what is really still owed, the 10.00 tail plus interest, because June's
    # PENDING leg is counted; nothing is left to pay in August.
    rate = Decimal("6.00") / Decimal("100") / Decimal("12")
    june_interest = ((pmt + Decimal("10.00")) * rate).quantize(Decimal("0.01"))
    owed_july = Decimal("10.00") + june_interest
    july_payment = owed_july + (owed_july * rate).quantize(Decimal("0.01"))
    assert row.loan_payments == [
        (datetime.date(2026, 5, 15), pmt),
        (datetime.date(2026, 7, 15), july_payment),
    ]
    # Never paid past zero.
    assert row.end_balance == Decimal("0.00")
    assert min(b for _, b in row.points) < 0 and max(b for _, b in row.points) <= 0
    # The source pays the two synthesised payments and the booked June leg.
    src = next(a for a in kb.accounts if a.account.id == source.id)
    assert src.end_balance == src.balance - pmt - pmt - july_payment


async def test_loan_amortises_to_term_on_a_long_horizon(db_session):
    """Interest accrues every period after the first, so a 20k loan at 7%
    over 60 months is paid for all 60 months, not stopped after ~51 by
    interest-blind paydown, and the last payment closes it at zero."""
    org, _, source = await _org_with_source(db_session)
    principal = Decimal("20000.00")
    pmt = compute_pmt(principal, Decimal("7.00"), 60)
    loan = await _loan(db_session, org, source, owed=principal, principal=principal,
                       apr=Decimal("7.00"), term=60, first_payment=datetime.date(2026, 5, 15))
    await db_session.commit()

    kb = await projection_kernel.baseline(
        db_session, org.id, today=P_START, horizon_end=datetime.date(2032, 12, 31),
    )
    row = next(a for a in kb.accounts if a.account.id == loan.id)
    assert len(row.loan_payments) == 60
    assert row.loan_payments[-1][0] == datetime.date(2031, 4, 15)
    assert all(amount == pmt for _, amount in row.loan_payments[:-1])
    assert Decimal("0") < row.loan_payments[-1][1] <= pmt
    assert row.end_balance == Decimal("0.00")


async def test_fixed_amount_card_pays_every_cycle_capped_at_owed(db_session):
    """1000 owed at a fixed 200 pays 200 on five due dates, then nothing; the
    last cycle never pays past what is owed."""
    org, _, source = await _org_with_source(db_session)
    cc = await _card(db_session, org, source, owed=Decimal("1000.00"), fixed=Decimal("200.00"))
    cat = Category(org_id=org.id, name="Shop", slug="shop", type=CategoryType.EXPENSE)
    db_session.add(cat)
    await db_session.flush()
    db_session.add(_tx(org.id, cc, cat, "1000.00", datetime.date(2026, 4, 10), settled=True))
    await db_session.commit()

    kb = await projection_kernel.baseline(
        db_session, org.id, today=P_START, horizon_end=datetime.date(2026, 12, 31),
    )
    row = next(a for a in kb.accounts if a.account.id == cc.id)
    assert row.cc_payments == [
        (datetime.date(2026, m, 1), Decimal("200.00")) for m in (5, 6, 7, 8, 9)
    ]
    assert row.end_balance == Decimal("0.00")


async def test_card_payments_run_for_ten_years(db_session):
    """The cycle walk is sized from the horizon: a fixed 10.00 on 2000 owed
    pays on every one of 120 monthly due dates."""
    org, _, source = await _org_with_source(db_session)
    cc = await _card(db_session, org, source, owed=Decimal("2000.00"), fixed=Decimal("10.00"))
    cat = Category(org_id=org.id, name="Shop", slug="shop", type=CategoryType.EXPENSE)
    db_session.add(cat)
    await db_session.flush()
    db_session.add(_tx(org.id, cc, cat, "2000.00", datetime.date(2026, 4, 10), settled=True))
    await db_session.commit()

    kb = await projection_kernel.baseline(
        db_session, org.id, today=P_START, horizon_end=datetime.date(2036, 4, 30),
    )
    row = next(a for a in kb.accounts if a.account.id == cc.id)
    assert len(row.cc_payments) == 120
    assert row.cc_payments[-1] == (datetime.date(2036, 4, 1), Decimal("10.00"))


def test_card_cycle_walk_cap_logs_an_error(monkeypatch, log_lines):
    """A resolver that never advances hits the cap, and that is logged at
    ERROR rather than silently truncating the horizon."""
    stuck = cc_forecast_service.resolve_cycle_for_account(
        type("Card", (), {"close_day": 25, "payment_day": 1,
                          "payment_day_relative_month": 1, "id": 7})(),
        datetime.date(2026, 5, 1),
    )
    monkeypatch.setattr(cc_forecast_service, "resolve_cycle_for_account",
                        lambda account, cursor: stuck)
    account = type("Card", (), {"id": 7, "payment_day_relative_month": 1})()
    cc_forecast_service.due_cycles_in_horizon(
        account, p_start=datetime.date(2026, 5, 1), p_end=datetime.date(2027, 5, 1),
    )
    hits = [e for e in log_lines() if e["msg"] == "cc_forecast.cycle_walk_cap_hit"]
    assert hits and hits[0]["level"] == "ERROR"


async def test_excluded_series_drops_its_pending_card_charge(db_session):
    """An excluded series' generated PENDING charge on a card leaves the
    card's ledger too, so the projected payment no longer pays for it."""
    org, _, source = await _org_with_source(db_session)
    cc = await _card(db_session, org, source, owed=Decimal("100.00"))
    cat = Category(org_id=org.id, name="Shop", slug="shop", type=CategoryType.EXPENSE)
    db_session.add(cat)
    await db_session.flush()
    gym = RecurringTransaction(
        org_id=org.id, account_id=cc.id, category_id=cat.id, description="gym",
        amount=Decimal("300.00"), type="expense", frequency="monthly",
        next_due_date=datetime.date(2026, 5, 20), auto_settle=False, is_active=True,
    )
    db_session.add(gym)
    await db_session.flush()
    db_session.add_all([
        _tx(org.id, cc, cat, "100.00", datetime.date(2026, 4, 10), settled=True),
        _tx(org.id, cc, cat, "300.00", datetime.date(2026, 4, 20), recurring_id=gym.id),
    ])
    await db_session.commit()

    async def may_payment(**kw):
        kb = await projection_kernel.baseline(
            db_session, org.id, today=P_START, horizon_end=P_END, **kw,
        )
        return next(a for a in kb.accounts if a.account.id == cc.id).cc_payments

    assert await may_payment() == [(P_START, Decimal("400.00"))]
    assert await may_payment(exclude_recurring_ids=[gym.id]) == [(P_START, Decimal("100.00"))]


async def test_default_window_keeps_overdue_obligations_on_day_zero(db_session):
    """With no ``window_start`` the kernel uses the current billing period, as
    the dashboard does: a pending row and an un-materialised occurrence dated
    before today are floored onto day 0, not dropped, and the month-end
    equals the dashboard's for every account."""
    seed = await _seed(db_session)
    org_id = seed["org_id"]
    cat = Category(org_id=org_id, name="Late", slug="late", type=CategoryType.EXPENSE)
    db_session.add(cat)
    await db_session.flush()
    db_session.add_all([
        _tx(org_id, seed["eur_b"], cat, "20.00", datetime.date(2026, 5, 5)),
        RecurringTransaction(
            org_id=org_id, account_id=seed["eur_b"].id, category_id=cat.id,
            description="late", amount=Decimal("30.00"), type="expense",
            frequency="monthly", next_due_date=datetime.date(2026, 5, 3),
            occurrence_count=1, auto_settle=False, is_active=True,
        ),
    ])
    await db_session.commit()

    kb = await projection_kernel.baseline(
        db_session, org_id, today=TODAY, horizon_end=datetime.date(2026, 7, 31),
    )
    eur_b = next(a for a in kb.accounts if a.account.id == seed["eur_b"].id)
    assert kb.walk_start == TODAY
    # Day 0 already carries both overdue items: 300 - 20 - 30.
    assert eur_b.points[0] == (TODAY, Decimal("250.00"))

    fc = await compute_account_balance_forecast(db_session, org_id, today=TODAY)
    for row in fc["accounts"]:
        mine = next(a for a in kb.accounts if a.account.id == row["account_id"])
        assert str(_at(mine.points, P_END)) == row["expected_month_end_balance"]


async def test_daily_points_cover_an_over_long_first_period(db_session):
    """A first period longer than 24 months stays daily to its end, so no
    in-month dip inside it is hidden from the low-balance read."""
    seed = await _seed(db_session)
    first_end = datetime.date(2028, 10, 31)
    kb = await projection_kernel.baseline(
        db_session, seed["org_id"], today=P_START, window_start=P_START,
        first_period_end=first_end, horizon_end=datetime.date(2029, 1, 15),
    )
    dates = [d for d, _ in kb.accounts[0].points]
    i = dates.index(first_end)
    assert i == (first_end - P_START).days
    assert dates[i + 1:] == [
        datetime.date(2028, 11, 30), datetime.date(2028, 12, 31), datetime.date(2029, 1, 15),
    ]


async def test_occurrence_count_is_respected_over_a_long_horizon(db_session):
    """A 12-instalment series projects exactly its remaining instalments over a
    ten-year horizon, never one more."""
    seed = await _seed(db_session)
    cat = Category(org_id=seed["org_id"], name="Plan", slug="plan", type=CategoryType.EXPENSE)
    db_session.add(cat)
    await db_session.flush()
    db_session.add(RecurringTransaction(
        org_id=seed["org_id"], account_id=seed["eur_b"].id, category_id=cat.id,
        description="tv", amount=Decimal("49.00"), type="expense", frequency="monthly",
        next_due_date=datetime.date(2026, 5, 12), occurrence_count=12,
        occurrences_elapsed=3, auto_settle=False, is_active=True,
    ))
    await db_session.commit()

    kb = await projection_kernel.baseline(
        db_session, seed["org_id"], today=TODAY, horizon_end=datetime.date(2036, 5, 31),
    )
    eur_b = next(a for a in kb.accounts if a.account.id == seed["eur_b"].id)
    assert len(eur_b.recurring_lines) == 9
    assert eur_b.recurring_lines[-1] == (datetime.date(2027, 1, 12), Decimal("-49.00"))
