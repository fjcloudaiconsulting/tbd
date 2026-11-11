"""Fences for ``projection_kernel.baseline`` (TBD-599).

The kernel is the one multi-month projection the dashboard forecast and Plans
share. The dashboard's own behaviour is pinned by
``test_account_balance_forecast_service.py``, unchanged; these tests pin what
is new: the differential against the dashboard over a LONGER horizon, the
point schedule, the per-currency series, ``exclude_recurring_ids`` and loans
across several months.
"""
import datetime
from decimal import ROUND_HALF_UP, Decimal

import pytest
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
from sqlalchemy import select

from app.models.scenario import Scenario, ScenarioType
from app.schemas.recurring import RecurringCreate, RecurringUpdate
from app.services import (
    cc_forecast_service,
    forecast_service,
    projection_kernel,
    recurring_service,
    scenario_engine,
)
from app.services.exceptions import ValidationError
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


@pytest.mark.parametrize("transfer", [False, True])
async def test_differential_month_end_equals_dashboard_per_account_and_currency(
    db_session, transfer
):
    """DoD fence. With zero goals the kernel, run over a 4-month horizon,
    reads the dashboard's ``expected_month_end_balance`` at the period's
    month-end, per account AND per currency.

    TBD-600 (T7): also with a recurring transfer EUR A -> EUR B of 30 on the
    25th, which moves both accounts and leaves the EUR total alone."""
    seed = await _seed(db_session)
    shift = Decimal("30.00") if transfer else Decimal("0")
    if transfer:
        await _transfer_series(db_session, seed["org_id"], seed["eur_a"].id, seed["eur_b"].id,
                               amount=shift, next_due_date=datetime.date(2026, 5, 25))
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
    assert by_id[seed["eur_a"].id]["expected_month_end_balance"] == str(Decimal("600.00") - shift)
    assert by_id[seed["usd"].id]["expected_month_end_balance"] == "9500.00"
    assert by_id[seed["eur_b"].id]["expected_month_end_balance"] == str(Decimal("250.00") + shift)

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
    assert _at(kernel_rows[seed["eur_a"].id].points, datetime.date(2026, 6, 30)) == (
        Decimal("200.00") - 2 * shift
    )
    assert kernel_rows[seed["eur_a"].id].end_balance == Decimal("-600.00") - 4 * shift


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
    # Interest is visible, and is exactly what the pair fails to conserve.
    assert row.loan_interest == [
        (datetime.date(2026, 6, 15), june_interest),
        (datetime.date(2026, 7, 15), july_payment - owed_july),
    ]
    assert (src.end_balance - src.balance) + (row.end_balance - row.balance) == -sum(
        amount for _, amount in row.loan_interest)


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
    src = next(a for a in kb.accounts if a.account.id == source.id)
    assert len(row.loan_interest) == 59  # every period after the first
    assert (src.end_balance - src.balance) + (row.end_balance - row.balance) == -sum(
        amount for _, amount in row.loan_interest)


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


@pytest.mark.parametrize("day", [29, 30, 31])
async def test_loan_anchored_late_in_the_month_pays_every_month(db_session, day):
    """A first period ending on the 29th-31st and a loan due on the same day:
    every month through February and after holds exactly one due date, so
    the loan pays (and accrues) every month instead of about half of them."""
    org, _, source = await _org_with_source(db_session)
    first_end = datetime.date(2027, 1, day)
    loan = await _loan(db_session, org, source, owed=Decimal("20000.00"),
                       principal=Decimal("20000.00"), apr=Decimal("7.00"), term=60,
                       first_payment=first_end)
    await db_session.commit()

    kb = await projection_kernel.baseline(
        db_session, org.id, today=datetime.date(2027, 1, 1),
        window_start=datetime.date(2027, 1, 1), first_period_end=first_end,
        horizon_end=datetime.date(2027, 7, 31),
    )
    row = next(a for a in kb.accounts if a.account.id == loan.id)
    expected = [first_end + relativedelta(months=k) for k in range(7)]
    assert [d for d, _ in row.loan_payments] == expected
    assert [d for d, _ in row.loan_interest] == expected[1:]


async def test_card_subscription_is_paid_off_every_cycle(db_session):
    """15.00 a month charged to a card, never generated yet: each cycle due
    after the first period bills it and the source pays it, so the card does
    not drift into debt over twelve months."""
    org, _, source = await _org_with_source(db_session)
    cc = await _card(db_session, org, source, owed=Decimal("0.00"))
    cat = Category(org_id=org.id, name="Music", slug="music", type=CategoryType.EXPENSE)
    db_session.add(cat)
    await db_session.flush()
    db_session.add(RecurringTransaction(
        org_id=org.id, account_id=cc.id, category_id=cat.id, description="music",
        amount=Decimal("15.00"), type="expense", frequency="monthly",
        next_due_date=datetime.date(2026, 5, 10), auto_settle=False, is_active=True,
    ))
    await db_session.commit()

    kb = await projection_kernel.baseline(
        db_session, org.id, today=P_START, horizon_end=datetime.date(2027, 4, 30),
    )
    row = next(a for a in kb.accounts if a.account.id == cc.id)
    # May 2026 .. March 2027 charges, each paid on the 1st after its close.
    assert row.cc_payments == [
        (datetime.date(2026, 6, 1) + relativedelta(months=k), Decimal("15.00"))
        for k in range(11)
    ]
    # Only April 2027's charge is still on the card at the horizon.
    assert row.end_balance == Decimal("-15.00")


async def test_default_window_without_an_open_period_matches_the_dashboard(db_session):
    """No open period, ``billing_cycle_day=31``, today 2027-03-05: the kernel's
    read-only default window is exactly the period the dashboard creates."""
    org = Organization(name="Late cycle", billing_cycle_day=31)
    db_session.add(org)
    await db_session.commit()
    today = datetime.date(2027, 3, 5)

    window = await projection_kernel.current_window(db_session, org.id, today=today)
    assert (await db_session.execute(
        BillingPeriod.__table__.select().where(BillingPeriod.org_id == org.id)
    )).first() is None  # read-only: nothing was created

    fc = await compute_account_balance_forecast(db_session, org.id, today=today)
    assert window == (
        datetime.date.fromisoformat(fc["period_start"]),
        datetime.date.fromisoformat(fc["period_end"]),
    )
    assert window == (datetime.date(2027, 2, 28), datetime.date(2027, 3, 27))


# ── Recurring transfers (TBD-600) ───────────────────────────────────────────


async def _transfer_series(db, org_id, source_id, dest_id, *, amount=Decimal("100.00"),
                           next_due_date=datetime.date(2026, 5, 20), **overrides):
    """A transfer template through the service, so it is validated as a user's."""
    cat = await db.scalar(select(Category).where(
        Category.org_id == org_id, Category.slug == "transfer"
    ))
    if cat is None:
        cat = Category(org_id=org_id, name="Transfer", slug="transfer",
                       type=CategoryType.BOTH, is_system=True)
        db.add(cat)
        await db.commit()
    body = dict(account_id=source_id, to_account_id=dest_id, category_id=cat.id,
                description="To savings", amount=amount, type="transfer",
                frequency="monthly", next_due_date=next_due_date, auto_settle=False)
    body.update(overrides)
    r = await recurring_service.create_recurring(db, org_id, RecurringCreate(**body), today=TODAY)
    return r.id


async def _two_accounts(db) -> dict:
    org = Organization(name="Xfer", billing_cycle_day=1)
    db.add(org)
    await db.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    card_t = AccountType(org_id=org.id, name="Card", slug="credit_card", is_system=True)
    loan_t = AccountType(org_id=org.id, name="Loan", slug="loan", is_system=True)
    db.add_all([at, card_t, loan_t])
    await db.flush()
    main = Account(org_id=org.id, name="Main", account_type_id=at.id,
                   balance=Decimal("1000.00"), currency="EUR", is_default=True)
    savings = Account(org_id=org.id, name="Savings", account_type_id=at.id,
                      balance=Decimal("0.00"), currency="EUR", is_default=False)
    card = Account(org_id=org.id, name="Card", account_type_id=card_t.id,
                   balance=Decimal("0.00"), currency="EUR", is_default=False)
    loan = Account(org_id=org.id, name="Loan", account_type_id=loan_t.id,
                   balance=Decimal("0.00"), currency="EUR", is_default=False)
    food = Category(org_id=org.id, name="Food", slug="food", type=CategoryType.EXPENSE)
    db.add_all([main, savings, card, loan, food])
    db.add(BillingPeriod(org_id=org.id, start_date=P_START, end_date=P_END))
    await db.flush()
    # Some real spending, so the forecast fence is not all zeros.
    db.add(RecurringTransaction(
        org_id=org.id, account_id=main.id, category_id=food.id, description="food",
        amount=Decimal("40.00"), type="expense", frequency="monthly",
        next_due_date=datetime.date(2026, 5, 15), auto_settle=False, is_active=True,
    ))
    await db.commit()
    return {"org_id": org.id, "main": main.id, "savings": savings.id,
            "card": card.id, "loan": loan.id}


def _points_by_account(kb) -> dict[int, list]:
    return {a.account.id: a.points for a in kb.accounts}


async def test_transfer_conserves_and_moves_both_accounts(db_session):
    """T1. Against the same books without the series, over 3 months: the EUR
    series is identical at every point, and after the k-th occurrence the
    source is k x amount lower and the destination k x amount higher."""
    seed = await _two_accounts(db_session)
    rid = await _transfer_series(db_session, seed["org_id"], seed["main"], seed["savings"])
    kw = dict(today=TODAY, horizon_end=datetime.date(2026, 7, 31), window_start=P_START)
    with_ = await projection_kernel.baseline(db_session, seed["org_id"], **kw)
    without = await projection_kernel.baseline(
        db_session, seed["org_id"], exclude_recurring_ids=[rid], **kw
    )
    assert with_.currencies["EUR"] == without.currencies["EUR"]
    occurrences = [datetime.date(2026, m, 20) for m in (5, 6, 7)]
    a, b = _points_by_account(with_), _points_by_account(without)
    for account_id, sign in ((seed["main"], -1), (seed["savings"], 1)):
        for (day, got), (_, base) in zip(a[account_id], b[account_id]):
            k = sum(1 for o in occurrences if o <= day)
            assert got - base == sign * k * Decimal("100.00"), (account_id, day)
    assert a[seed["main"]][-1][1] - b[seed["main"]][-1][1] == Decimal("-300.00")


async def test_transfer_points_survive_generation_and_auto_settle(db_session):
    """T2. Per-account points are identical before generation, with the
    pending pair generated, and with the pair auto-settled."""
    seed = await _two_accounts(db_session)
    await _transfer_series(db_session, seed["org_id"], seed["main"], seed["savings"],
                           auto_settle=True)
    kw = dict(today=TODAY, horizon_end=datetime.date(2026, 7, 31), window_start=P_START)

    async def points():
        return _points_by_account(await projection_kernel.baseline(db_session, seed["org_id"], **kw))

    async def legs():
        return (await db_session.execute(
            select(Transaction).where(
                Transaction.org_id == seed["org_id"],
                Transaction.linked_transaction_id.is_not(None),
            ).execution_options(populate_existing=True)
        )).scalars().all()

    before = await points()
    await recurring_service.generate_due_transactions(db_session, seed["org_id"], today=TODAY)
    assert [t.status for t in await legs()] == [TransactionStatus.PENDING] * 2
    pending = await points()
    await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=datetime.date(2026, 5, 20)
    )
    assert [t.status for t in await legs()] == [TransactionStatus.SETTLED] * 2
    settled = await points()
    for account_id in (seed["main"], seed["savings"]):
        assert before[account_id] == pending[account_id] == settled[account_id]


async def test_overdue_transfer_lands_on_walk_start_on_both_accounts(db_session):
    """T3. An un-generated occurrence dated before today is booked on day 0
    on both accounts, and each line keeps the occurrence date."""
    seed = await _two_accounts(db_session)
    await _transfer_series(db_session, seed["org_id"], seed["main"], seed["savings"],
                           next_due_date=datetime.date(2026, 5, 5))
    kb = await projection_kernel.baseline(
        db_session, seed["org_id"], today=TODAY, horizon_end=P_END, window_start=P_START,
    )
    rows = {a.account.id: a for a in kb.accounts}
    assert kb.walk_start == TODAY
    assert _at(rows[seed["savings"]].points, TODAY) == Decimal("100.00")
    # The food series is due May 15, after today, so day 0 holds the transfer alone.
    assert _at(rows[seed["main"]].points, TODAY) == Decimal("900.00")
    assert (datetime.date(2026, 5, 5), Decimal("-100.00")) in rows[seed["main"]].recurring_lines
    assert rows[seed["savings"]].recurring_lines == [(datetime.date(2026, 5, 5), Decimal("100.00"))]


async def test_occurrence_count_bounds_pairs_and_projections(db_session):
    """T4. ``occurrence_count=3``: three projected occurrences per account,
    and generation run month after month writes exactly three pairs."""
    seed = await _two_accounts(db_session)
    rid = await _transfer_series(db_session, seed["org_id"], seed["main"], seed["savings"],
                                 occurrence_count=3)
    kb = await projection_kernel.baseline(
        db_session, seed["org_id"], today=TODAY, horizon_end=datetime.date(2026, 10, 31),
        window_start=P_START,
    )
    rows = {a.account.id: a for a in kb.accounts}
    assert [d for d, _ in rows[seed["savings"]].recurring_lines] == [
        datetime.date(2026, m, 20) for m in (5, 6, 7)
    ]
    for month in range(5, 11):
        await recurring_service.generate_due_transactions(
            db_session, seed["org_id"], today=datetime.date(2026, month, 10)
        )
    legs = (await db_session.execute(select(Transaction).where(
        Transaction.recurring_id == rid
    ))).scalars().all()
    assert len(legs) == 6 and all(t.linked_transaction_id is not None for t in legs)


async def test_org_forecast_ignores_the_transfer(db_session):
    """T5. Income, expense and net of the org forecast are the same without
    the series, with it un-generated, and with it generated."""
    seed = await _two_accounts(db_session)
    keys = ("forecast_income", "forecast_expense", "forecast_net")

    async def numbers():
        fc = await forecast_service.compute_forecast(db_session, seed["org_id"], today=TODAY)
        return {k: fc[k] for k in keys}

    without = await numbers()
    assert Decimal(without["forecast_expense"]) == Decimal("40.00")  # anti-vacuity
    await _transfer_series(db_session, seed["org_id"], seed["main"], seed["savings"])
    assert await numbers() == without
    await recurring_service.generate_due_transactions(db_session, seed["org_id"], today=TODAY)
    assert await numbers() == without


@pytest.mark.parametrize("side", ["card", "loan"])
async def test_card_or_loan_is_refused_as_source_only(db_session, side):
    """T6 (TBD-789). A card or a loan as SOURCE is refused (a cash advance is
    not supported), on create and on update; as destination it is accepted,
    on create and on update."""
    seed = await _two_accounts(db_session)
    org_id, main, savings, other = seed["org_id"], seed["main"], seed["savings"], seed[side]
    with pytest.raises(ValidationError, match="credit card or loan"):
        await _transfer_series(db_session, org_id, other, savings)
    await db_session.rollback()
    rid = await _transfer_series(db_session, org_id, main, savings)
    with pytest.raises(ValidationError, match="credit card or loan"):
        await recurring_service.update_recurring(
            db_session, org_id, rid, RecurringUpdate(account_id=other), today=TODAY)
    await db_session.rollback()
    r = await recurring_service.update_recurring(
        db_session, org_id, rid, RecurringUpdate(to_account_id=other), today=TODAY)
    assert r.to_account_id == other
    await _transfer_series(db_session, org_id, main, other)


async def test_scenario_org_total_is_unchanged_by_the_transfer(db_session):
    """T8. The scenario engine's org total, month by month, is the same with
    the transfer series as without it, while the destination grows."""
    seed = await _two_accounts(db_session)
    rid = await _transfer_series(db_session, seed["org_id"], seed["main"], seed["savings"])
    state = await scenario_engine.build_world_state(db_session, org_id=seed["org_id"], user_id=0)
    without = scenario_engine.WorldState(
        accounts=state.accounts, recurring=[r for r in state.recurring if r.id != rid],
        history=state.history,
    )
    scenario = Scenario(
        org_id=seed["org_id"], user_id=0, name="baseline", scenario_type=ScenarioType.CUSTOM,
        params_json={"scenario_type": "custom", "label": "t", "events": []}, horizon_months=6,
    )

    def project(world):
        result = scenario_engine.AnalyticEngine().simulate(
            scenario_engine.SimulationRequest(scenario=scenario, state=world,
                                              horizon_months=6, options={})
        )
        per_account = {
            s["account_id"]: [Decimal(p["projected_balance"]) for p in s["points"]]
            for s in result["per_account_series"]
        }
        months = len(next(iter(per_account.values())))
        return [sum(v[i] for v in per_account.values()) for i in range(months)], per_account

    with_total, with_accounts = project(state)
    without_total, without_accounts = project(without)
    assert with_total == without_total
    assert with_accounts[seed["savings"]][-1] > without_accounts[seed["savings"]][-1]


async def test_destination_archived_later_projects_the_source_leg_only(db_session):
    """Documented behaviour: a destination archived after the series was set
    up drops out of the projection, and the source leg still projects (per-
    account truth). The series itself keeps running."""
    seed = await _two_accounts(db_session)
    await _transfer_series(db_session, seed["org_id"], seed["main"], seed["savings"])
    (await db_session.get(Account, seed["savings"])).is_active = False
    await db_session.commit()
    kb = await projection_kernel.baseline(
        db_session, seed["org_id"], today=TODAY, horizon_end=P_END, window_start=P_START,
    )
    rows = {a.account.id: a for a in kb.accounts}
    assert seed["savings"] not in rows
    assert (datetime.date(2026, 5, 20), Decimal("-100.00")) in rows[seed["main"]].recurring_lines


# ── Recurring transfers into a card or a loan (TBD-789) ─────────────────────


async def _bill_card(db, seed, *, fixed=None):
    """Seed's card closes on the 5th and is due on the 25th of the same month,
    paid from Main, with 500.00 charged before the May close."""
    card = await db.get(Account, seed["card"])
    card.balance, card.opening_balance = Decimal("-500.00"), Decimal("0.00")
    card.close_day, card.payment_day, card.payment_day_relative_month = 5, 25, 0
    card.payment_source_account_id = seed["main"]
    card.payment_strategy = PaymentStrategy.FIXED_AMOUNT if fixed else PaymentStrategy.FULL_BALANCE
    card.fixed_payment_amount = fixed
    food = await db.scalar(select(Category).where(
        Category.org_id == seed["org_id"], Category.slug == "food"))
    db.add(_tx(seed["org_id"], card, food, "500.00", datetime.date(2026, 4, 28), settled=True))
    await db.commit()


async def _amortise_loan(db, seed, *, owed, principal=Decimal("10000.00")):
    """Seed's loan: ``principal`` at 6% over 60 months, due on the 15th from
    May, paid from Main, ``owed`` still owed."""
    loan = await db.get(Account, seed["loan"])
    loan.balance = loan.opening_balance = -owed
    loan.payment_source_account_id = seed["main"]
    loan.principal_amount, loan.interest_rate_apr, loan.term_months = (
        principal, Decimal("6.00"), 60)
    loan.origination_date = datetime.date(2026, 4, 15)
    loan.first_payment_date = datetime.date(2026, 5, 15)
    await db.commit()


async def _before_after_without(db, seed, rid, horizon_end, *, legs=2):
    """The kernel before generation, after it, and without the series."""
    kw = dict(today=TODAY, horizon_end=horizon_end, window_start=P_START)
    before = await projection_kernel.baseline(db, seed["org_id"], **kw)
    without = await projection_kernel.baseline(db, seed["org_id"], exclude_recurring_ids=[rid], **kw)
    await recurring_service.generate_due_transactions(db, seed["org_id"], today=TODAY)
    rows = (await db.scalars(select(Transaction).where(Transaction.recurring_id == rid))).all()
    assert len(rows) == legs  # anti-vacuity: May's occurrences are generated
    after = await projection_kernel.baseline(db, seed["org_id"], **kw)
    return before, after, without


def _row(kb, account_id):
    return next(a for a in kb.accounts if a.account.id == account_id)


@pytest.mark.parametrize("fixed, expected", [
    # Full balance: May owes 500, the 100 transfer pays part, and nothing is
    # left for June once May's payment and transfer are in.
    (None, [(datetime.date(2026, 5, 25), Decimal("400.00"))]),
    # Fixed 300: May pays 300 less the transfer; June pays what is still owed
    # less June's transfer; then the card is paid off.
    (Decimal("300.00"), [(datetime.date(2026, 5, 25), Decimal("200.00")),
                         (datetime.date(2026, 6, 25), Decimal("100.00"))]),
])
async def test_transfer_into_a_card_lowers_its_payment_and_never_jumps(db_session, fixed, expected):
    """100.00 a month from Main into a card owing 500.00 that Main also pays.
    The transfer is a payment toward the card: the synthesized payment of its
    cycle is lowered by it from the first period (not paid twice), the
    forecast is identical before and after generation, and the EUR total is
    the same with the series as without it (conservation)."""
    seed = await _two_accounts(db_session)
    await _bill_card(db_session, seed, fixed=fixed)
    rid = await _transfer_series(db_session, seed["org_id"], seed["main"], seed["card"])
    before, after, without = await _before_after_without(
        db_session, seed, rid, datetime.date(2026, 8, 31))

    assert _row(without, seed["card"]).cc_payments[0] == (
        datetime.date(2026, 5, 25), fixed or Decimal("500.00"))
    assert _row(before, seed["card"]).cc_payments == expected
    assert _row(after, seed["card"]).cc_payments == expected
    assert _points_by_account(before) == _points_by_account(after)
    assert before.currencies["EUR"] == without.currencies["EUR"] == after.currencies["EUR"]
    # The strategy never pays past zero; only June's own transfer takes the
    # full-balance card into credit.
    assert _at(_row(before, seed["card"]).points, datetime.date(2026, 6, 25)) == (
        Decimal("100.00") if fixed is None else Decimal("0.00"))


@pytest.mark.parametrize("size", ["below", "equal", "above"])
async def test_transfer_into_a_loan_is_the_payment_or_extra_principal(db_session, size):
    """A monthly transfer from Main into a loan Main also pays. Below the PMT
    it is extra principal on top of the scheduled payment; at or above the PMT
    it IS the scheduled payment (the excess is extra principal), so nothing
    else is paid. The forecast is the same before generation, after it, after
    a stop and after a delete (``recurring_id`` is cleared by both); the legs
    lower the interest basis; and only interest leaves the EUR total."""
    seed = await _two_accounts(db_session)
    owed = Decimal("10000.00")
    await _amortise_loan(db_session, seed, owed=owed)
    pmt = compute_pmt(Decimal("10000.00"), Decimal("6.00"), 60)
    amount = {"below": Decimal("100.00"), "equal": pmt, "above": pmt + Decimal("100.00")}[size]
    rid = await _transfer_series(db_session, seed["org_id"], seed["main"], seed["loan"],
                                 amount=amount)
    before, after, without = await _before_after_without(
        db_session, seed, rid, datetime.date(2026, 8, 31))

    scheduled = [(datetime.date(2026, m, 15), pmt) for m in (5, 6, 7, 8)]
    assert _row(without, seed["loan"]).loan_payments == scheduled
    expected = scheduled if size == "below" else []
    assert _row(before, seed["loan"]).loan_payments == expected
    assert _row(after, seed["loan"]).loan_payments == expected
    assert _points_by_account(before) == _points_by_account(after)
    assert _row(before, seed["loan"]).loan_interest == _row(after, seed["loan"]).loan_interest
    # June accrues on what is owed after everything May paid in.
    paid_in_may = pmt + amount if size == "below" else amount
    rate = Decimal("6.00") / Decimal("100") / Decimal("12")
    june = ((owed - paid_in_may) * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert _row(before, seed["loan"]).loan_interest[0] == (datetime.date(2026, 6, 15), june)
    interest = {id(kb): sum(i for _, i in _row(kb, seed["loan"]).loan_interest)
                for kb in (before, without)}
    assert before.currencies["EUR"][-1][1] - without.currencies["EUR"][-1][1] == (
        interest[id(without)] - interest[id(before)])

    # The May pair outlives a stop and a delete (it is dated before the wall
    # clock, so neither removes it) and keeps reading the same way.
    async def may():
        kb = await projection_kernel.baseline(
            db_session, seed["org_id"], today=TODAY, horizon_end=P_END, window_start=P_START)
        return _points_by_account(kb), _row(kb, seed["loan"]).loan_payments

    generated = await may()
    assert generated[1] == expected[:1]
    await recurring_service.stop_recurring(db_session, seed["org_id"], rid)
    legs = (await db_session.scalars(select(Transaction).where(
        Transaction.recurring_origin_id == rid).execution_options(populate_existing=True))).all()
    assert len(legs) == 2 and all(t.recurring_id is None for t in legs)  # anti-vacuity
    assert await may() == generated
    await recurring_service.delete_recurring(db_session, seed["org_id"], rid)
    assert await may() == generated


async def test_extra_principal_never_pays_a_loan_past_zero(db_session):
    """The last scheduled payment is capped at what the period's extra
    principal leaves owed, so the loan closes at zero, not in credit."""
    seed = await _two_accounts(db_session)
    pmt = compute_pmt(Decimal("10000.00"), Decimal("6.00"), 60)
    await _amortise_loan(db_session, seed, owed=pmt + Decimal("50.00"))
    await _transfer_series(db_session, seed["org_id"], seed["main"], seed["loan"])
    kb = await projection_kernel.baseline(
        db_session, seed["org_id"], today=TODAY, horizon_end=P_END, window_start=P_START,
    )
    loan = _row(kb, seed["loan"])
    assert loan.loan_payments == [(datetime.date(2026, 5, 15), pmt - Decimal("50.00"))]
    assert loan.end_balance == Decimal("0.00")


# 20,000.00 at 6% over 60 months: a PMT of 386.66, two legs of 193.33.
_BIG = Decimal("20000.00")


@pytest.mark.parametrize("delta, paid_by_legs", [
    (Decimal("0.00"), True), (Decimal("10.00"), True), (Decimal("-10.00"), False),
])
async def test_biweekly_legs_into_a_loan_are_summed_against_the_pmt(db_session, delta, paid_by_legs):
    """Two biweekly legs a month (May 12 and 26, ...). Summed, at or above the
    PMT they are the scheduled payment; below it they are extra principal on
    top of it. Judged one by one, every case would also book the PMT. The
    forecast is the same before and after generation."""
    seed = await _two_accounts(db_session)
    await _amortise_loan(db_session, seed, owed=_BIG, principal=_BIG)
    pmt = compute_pmt(_BIG, Decimal("6.00"), 60)
    assert pmt == Decimal("386.66")
    rid = await _transfer_series(db_session, seed["org_id"], seed["main"], seed["loan"],
                                 amount=pmt / 2 + delta / 2, frequency="biweekly",
                                 next_due_date=datetime.date(2026, 5, 12))
    before, after, without = await _before_after_without(
        db_session, seed, rid, datetime.date(2026, 8, 31), legs=4)
    scheduled = _row(without, seed["loan"]).loan_payments
    assert len(scheduled) == 4
    expected = [] if paid_by_legs else scheduled
    assert _row(before, seed["loan"]).loan_payments == expected
    assert _row(after, seed["loan"]).loan_payments == expected
    assert _points_by_account(before) == _points_by_account(after)


async def test_a_generated_and_a_projected_leg_are_summed_together(db_session):
    """Two monthly series of 200.00 into a loan with a 386.66 PMT. With one
    May occurrence generated and the other still projected, May is paid by
    the two (no PMT booked), exactly as with both projected."""
    seed = await _two_accounts(db_session)
    await _amortise_loan(db_session, seed, owed=_BIG, principal=_BIG)
    one = await _transfer_series(db_session, seed["org_id"], seed["main"], seed["loan"],
                                 amount=Decimal("200.00"))
    await _transfer_series(db_session, seed["org_id"], seed["main"], seed["loan"],
                           amount=Decimal("200.00"), next_due_date=datetime.date(2026, 5, 25))
    kw = dict(today=TODAY, horizon_end=P_END, window_start=P_START)
    projected = await projection_kernel.baseline(db_session, seed["org_id"], **kw)
    await recurring_service.materialise_next(
        db_session, seed["org_id"], one, occurrence_date=datetime.date(2026, 5, 20),
        skipped=False, today=TODAY)
    assert len((await db_session.scalars(select(Transaction).where(
        Transaction.recurring_id == one))).all()) == 2  # anti-vacuity
    mixed = await projection_kernel.baseline(db_session, seed["org_id"], **kw)
    assert _row(projected, seed["loan"]).loan_payments == []
    assert _row(mixed, seed["loan"]).loan_payments == []
    assert _points_by_account(mixed) == _points_by_account(projected)


async def test_a_leg_without_an_origin_still_counts_as_recurring(db_session):
    """A leg written before ``recurring_origin_id`` existed (an old pod during
    the rollout, or a row the backfill missed) is still read through
    ``recurring_id``: a 100.00 leg stays extra principal and the PMT is still
    booked, rather than reading as a manual payment."""
    seed = await _two_accounts(db_session)
    await _amortise_loan(db_session, seed, owed=Decimal("10000.00"))
    rid = await _transfer_series(db_session, seed["org_id"], seed["main"], seed["loan"])
    kw = dict(today=TODAY, horizon_end=P_END, window_start=P_START)
    await recurring_service.generate_due_transactions(db_session, seed["org_id"], today=TODAY)
    with_origin = await projection_kernel.baseline(db_session, seed["org_id"], **kw)
    legs = (await db_session.scalars(select(Transaction).where(Transaction.recurring_id == rid))).all()
    assert len(legs) == 2
    for t in legs:
        t.recurring_origin_id = None
    await db_session.commit()
    without_origin = await projection_kernel.baseline(db_session, seed["org_id"], **kw)
    assert len(_row(with_origin, seed["loan"]).loan_payments) == 1
    assert _points_by_account(without_origin) == _points_by_account(with_origin)


async def test_legacy_currency_spellings_share_a_series_and_pay_their_card_and_loan(db_session):
    """Fence (TBD-801). ``accounts.currency`` was free text before TBD-325, so a
    legacy 'eur' card or 'EUR ' loan paid from an 'EUR' source is the same
    currency: it still gets its projected payments, and every account lands
    in ONE 'EUR' series. Kills a raw-string guard (no payments) and a raw key
    (three series)."""
    org, _, source = await _org_with_source(db_session)
    cc = await _card(db_session, org, source, owed=Decimal("1000.00"), fixed=Decimal("200.00"))
    loan = await _loan(db_session, org, source, owed=Decimal("5000.00"),
                       principal=Decimal("12000.00"), apr=Decimal("6.00"), term=60,
                       first_payment=datetime.date(2026, 5, 15))
    cc.currency, loan.currency = "eur", "EUR "
    cat = Category(org_id=org.id, name="Shop", slug="shop", type=CategoryType.EXPENSE)
    db_session.add(cat)
    await db_session.flush()
    db_session.add(_tx(org.id, cc, cat, "1000.00", datetime.date(2026, 4, 10), settled=True))
    await db_session.commit()

    kb = await projection_kernel.baseline(
        db_session, org.id, today=P_START, horizon_end=datetime.date(2026, 7, 31),
    )
    rows = {a.account.id: a for a in kb.accounts}
    assert rows[cc.id].cc_payments[0] == (datetime.date(2026, 5, 1), Decimal("200.00"))
    assert rows[loan.id].loan_payments[0][0] == datetime.date(2026, 5, 15)
    assert set(kb.currencies) == {"EUR"}
    assert kb.currencies["EUR"] == [
        (d, a + b + c) for (d, a), (_, b), (_, c)
        in zip(rows[source.id].points, rows[cc.id].points, rows[loan.id].points)
    ]
