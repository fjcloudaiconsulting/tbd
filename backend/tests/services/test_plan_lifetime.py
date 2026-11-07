"""Fences for the lifetime tier (TBD-563): age axis, compounding, ending flows,
depletion age and the required monthly contribution."""
import datetime
from decimal import Decimal

from dateutil.relativedelta import relativedelta

from app.models import Account, AccountType
from app.services import plan_projection_service as pps
from app.services.loan_service import compute_pmt
from tests.services import test_plan_projection_service as base
from tests.services.test_plan_projection_service import (
    TODAY,
    _books,
    _goal,
    _plan,
    _series,
)

db_session = base.db_session  # the same in-memory books fixture

D = Decimal
CENT = D("0.02")


async def _lifetime(db, books, goals=(), **kw):
    kw.setdefault("birth_year", None)
    return await pps.lifetime(db, books["org_id"], [_plan(books["org_id"])], list(goals),
                              today=TODAY, **kw)


def _flows(values, opening, r):
    """Monthly flows back out of a compounded series."""
    out, previous = [], opening
    for v in values:
        out.append(v - previous * (1 + r))
        previous = v
    return out


async def test_no_birth_year_renders_a_date_axis(db_session):
    books = await _books(db_session)
    result = await _lifetime(db_session, books)
    assert len(result.months) == 480
    assert set(result.ages) == {None}
    assert result.depletion_age is None


async def test_birth_year_sets_the_domain_and_labels_ages(db_session):
    books = await _books(db_session)
    result = await _lifetime(db_session, books, birth_year=1960)
    assert result.months[0] == datetime.date(2026, 5, 1)
    assert result.months[-1] == datetime.date(2060, 12, 1)
    assert result.ages[0] == 66 and result.ages[-1] == 100


async def test_compounding_has_no_kink_at_month_24(db_session):
    books = await _books(db_session)
    db_session.add(_series(books["org_id"], books["main"], books["food"], "50.00",
                           datetime.date(2026, 5, 20)))
    await db_session.commit()
    r = pps._monthly_rate(D("6"))
    result = await _lifetime(db_session, books, return_pct=D("6"))
    flows = _flows(result.nominal, D("1000.00"), r)
    for m in range(1, 40):
        assert abs(flows[m] + D("50.00")) <= CENT, m


async def test_a_liability_balance_never_compounds(db_session):
    books = await _books(db_session)
    card = await db_session.get(Account, books["card"])
    card.balance = D("-5000.00")
    await db_session.commit()
    r = pps._monthly_rate(D("12"))
    result = await _lifetime(db_session, books, return_pct=D("12"))
    # Main's 1000 (savings holds 0) grows; the card's owed balance is not in the pool.
    assert result.nominal[0] == (D("1000.00") * (1 + r)).quantize(D("0.01"))


async def test_a_loan_ending_at_month_30_stops_at_month_30(db_session):
    books = await _books(db_session)
    loan_t = AccountType(org_id=books["org_id"], name="Loan", slug="loan", is_system=True)
    db_session.add(loan_t)
    await db_session.flush()
    principal, first = D("3100.00"), datetime.date(2026, 5, 15)
    db_session.add(Account(
        org_id=books["org_id"], name="Loan", account_type_id=loan_t.id, balance=-principal,
        opening_balance=-principal, currency="EUR", is_default=False,
        payment_source_account_id=books["main"], principal_amount=principal,
        interest_rate_apr=D("0.00"), term_months=31,
        origination_date=first - relativedelta(months=1), first_payment_date=first,
    ))
    await db_session.commit()
    pmt = compute_pmt(principal, D("0.00"), 31)
    result = await _lifetime(db_session, books)
    flows = _flows(result.nominal, D("1000.00"), D("0"))
    assert all(flows[m] == -pmt for m in range(24, 30))
    assert D("0") > flows[30] >= -pmt
    assert all(f == 0 for f in flows[31:60])


async def test_depletion_age(db_session):
    books = await _books(db_session)
    db_session.add(_series(books["org_id"], books["main"], books["food"], "100.00",
                           datetime.date(2026, 5, 20)))
    await db_session.commit()
    result = await _lifetime(db_session, books, birth_year=1980)
    # 1000 less 100 a month from May: below zero in the 11th month, March 2027.
    assert result.depletion_month == datetime.date(2027, 3, 1)
    assert result.depletion_age == 47


async def test_a_lump_lands_in_its_month_past_the_walk(db_session):
    books = await _books(db_session)
    result = await _lifetime(db_session, books, goals=[_goal(
        books["org_id"], kind="lump", target_amount=D("400.00"),
        target_date=datetime.date(2030, 6, 15))])
    june = result.months.index(datetime.date(2030, 6, 1))
    assert result.nominal[june - 1] == D("1000.00")
    assert result.nominal[june] == D("600.00")


def _bisect(flows, opening, r, t, ok):
    """Least c (to the cent) for which compounding ``flows`` plus c in months < t passes ``ok``."""
    lo, hi = D("0"), D("100000")
    while hi - lo > D("0.001"):
        c = (lo + hi) / 2
        balances = pps._grow(opening, [f + (c if m < t else 0) for m, f in enumerate(flows)], r)
        lo, hi = (lo, c) if ok(balances) else (c, hi)
    return hi


async def test_required_contribution_closed_form_matches_bisection(db_session):
    books = await _books(db_session)
    savings = await db_session.get(Account, books["savings"])
    savings.balance = D("20000.00")
    await db_session.commit()
    goals = [
        _goal(books["org_id"], gid=10, kind="savings_target", account_id=books["savings"],
              target_amount=D("60000.00"), target_date=datetime.date(2031, 12, 31)),
        _goal(books["org_id"], gid=11, kind="retirement", account_id=books["savings"],
              monthly=D("900.00"), target_date=datetime.date(2035, 1, 1)),
    ]
    r = pps._monthly_rate(D("5"))
    for goal in goals:
        result = await _lifetime(db_session, books, goals=[goal], birth_year=1975,
                                 return_pct=D("5"), inflation_pct=D("2"))
        (req,) = result.required
        trajectory = result.goal_accounts[books["savings"]]
        flows = _flows(trajectory, D("20000.00"), r)
        t = result.months.index(goal.target_date.replace(day=1))
        if goal.kind == "savings_target":
            def ok(b, t=t):
                return b[t] >= D("60000.00")
        else:
            def ok(b, t=t):
                return min(b[t:]) >= 0
        assert req.monthly > 0
        assert abs(req.monthly - _bisect(flows, D("20000.00"), r, t, ok)) <= D("0.05"), goal.kind


async def test_a_target_in_the_current_month_is_unreachable(db_session):
    books = await _books(db_session)
    result = await _lifetime(db_session, books, goals=[_goal(
        books["org_id"], kind="savings_target", account_id=books["savings"],
        target_amount=D("5000.00"), target_date=datetime.date(2026, 5, 31))])
    assert result.required == [pps.GoalRequirement(10, None)]


async def test_a_retirement_goal_without_an_account_draws_nothing_from_the_pool(db_session):
    books = await _books(db_session)
    result = await _lifetime(db_session, books, goals=[_goal(
        books["org_id"], kind="retirement", monthly=D("900.00"),
        target_date=datetime.date(2030, 1, 1))])
    assert set(result.nominal) == {D("1000.00")}
    assert result.required == []


async def test_retirement_draws_are_todays_money_inflated(db_session):
    books = await _books(db_session)
    savings = await db_session.get(Account, books["savings"])
    savings.balance = D("100000.00")
    await db_session.commit()
    result = await _lifetime(db_session, books, inflation_pct=D("12"), goals=[_goal(
        books["org_id"], kind="retirement", account_id=books["savings"], monthly=D("1000.00"),
        target_date=datetime.date(2030, 1, 1))])
    flows = _flows(result.goal_accounts[books["savings"]], D("100000.00"), D("0"))
    start = result.months.index(datetime.date(2030, 1, 1))
    i = pps._monthly_rate(D("12"))
    assert all(f == 0 for f in flows[:start])
    for m in (start, start + 30):
        assert abs(flows[m] + D("1000.00") * (1 + i) ** m) <= CENT
