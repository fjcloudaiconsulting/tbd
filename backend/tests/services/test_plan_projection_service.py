"""Fences for ``plan_projection_service`` (TBD-562): goal overlays composed over
the shared baseline, per-currency reconciliation, and goal toggles.

Plans and goals are transient rows: the service reads their fields, never their
tables, so only the books (accounts, series, rows) are persisted.
"""
import datetime
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization, Transaction
from app.models.base import Base
from app.models.billing import BillingPeriod
from app.models.category import CategoryType
from app.models.recurring import RecurringTransaction
from app.models.scenario import PlanGoal, Scenario
from app.models.transaction import TransactionStatus, TransactionType
from app.schemas.recurring import RecurringCreate
from app.services import plan_projection_service as pps
from app.services import recurring_service

P_START = datetime.date(2026, 5, 1)
P_END = datetime.date(2026, 5, 31)
TODAY = datetime.date(2026, 5, 10)
HORIZON = datetime.date(2026, 12, 31)
D = Decimal


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


async def _books(db) -> dict:
    """EUR main and savings accounts, a EUR card, a USD account worth more than
    everything else, and a plan category."""
    org = Organization(name="Plans", billing_cycle_day=1)
    db.add(org)
    await db.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    card_t = AccountType(org_id=org.id, name="Card", slug="credit_card", is_system=True)
    db.add_all([at, card_t])
    await db.flush()
    main = Account(org_id=org.id, name="Main", account_type_id=at.id,
                   balance=D("1000.00"), currency="EUR", is_default=True)
    savings = Account(org_id=org.id, name="Savings", account_type_id=at.id,
                      balance=D("0.00"), currency="EUR", is_default=False)
    card = Account(org_id=org.id, name="Card", account_type_id=card_t.id,
                   balance=D("0.00"), currency="EUR", is_default=False)
    usd = Account(org_id=org.id, name="USD", account_type_id=at.id,
                  balance=D("90000.00"), currency="USD", is_default=False)
    trip = Category(org_id=org.id, name="Trip", slug="trip", type=CategoryType.EXPENSE)
    other = Category(org_id=org.id, name="Food", slug="food", type=CategoryType.EXPENSE)
    db.add_all([main, savings, card, usd, trip, other])
    db.add(BillingPeriod(org_id=org.id, start_date=P_START, end_date=P_END))
    await db.commit()
    return {"org_id": org.id, "main": main.id, "savings": savings.id, "card": card.id,
            "usd": usd.id, "trip": trip.id, "food": other.id}


def _series(org_id, account_id, category_id, amount, next_due, *, frequency="monthly"):
    return RecurringTransaction(
        org_id=org_id, account_id=account_id, category_id=category_id, description="s",
        amount=D(amount), type="expense", frequency=frequency, next_due_date=next_due,
        auto_settle=False, is_active=True,
    )


async def _transfer(db, org_id, source, dest, amount="100.00", next_due=datetime.date(2026, 5, 20)):
    cat = await db.scalar(select(Category).where(
        Category.org_id == org_id, Category.slug == "transfer"))
    if cat is None:
        cat = Category(org_id=org_id, name="Transfer", slug="transfer",
                       type=CategoryType.BOTH, is_system=True)
        db.add(cat)
        await db.commit()
    r = await recurring_service.create_recurring(db, org_id, RecurringCreate(
        account_id=source, to_account_id=dest, category_id=cat.id, description="To savings",
        amount=D(amount), type="transfer", frequency="monthly", next_due_date=next_due,
        auto_settle=False,
    ), today=TODAY)
    return r.id


def _plan(org_id, *, pid=1, budget=None, category_id=None):
    return Scenario(id=pid, org_id=org_id, user_id=1, name="p", scenario_type="custom",
                    params_json={}, currency="EUR", monthly_budget=budget,
                    category_id=category_id)


def _goal(org_id, *, gid=10, pid=1, kind="savings_target", monthly=None, account_id=None,
          funding=None, enabled=True, target_amount=None, target_date=None):
    return PlanGoal(id=gid, org_id=org_id, scenario_id=pid, name="g", tier="essential",
                    kind=kind, monthly_amount=monthly, account_id=account_id,
                    funding_recurring_id=funding, enabled=enabled,
                    target_amount=target_amount, target_date=target_date)


async def _project(db, books, plans, goals):
    return await pps.project(db, books["org_id"], plans, goals, today=TODAY,
                             horizon_end=HORIZON, window_start=P_START)


def _month(result, month):
    return next(m for m in result.months if m.month == month)


# ── compose, pure ────────────────────────────────────────────────────────────

def test_compose_adds_each_delta_from_its_day_on():
    points = [(datetime.date(2026, 5, d), D("10")) for d in (10, 11, 12)]
    deltas = [(datetime.date(2026, 5, 1), D("-1")), (datetime.date(2026, 5, 11), D("-2")),
              (datetime.date(2027, 1, 1), D("-99"))]
    # Before the walk floors onto day 0; after the horizon is dropped.
    assert pps.compose(points, deltas) == [
        (datetime.date(2026, 5, 10), D("9")),
        (datetime.date(2026, 5, 11), D("7")),
        (datetime.date(2026, 5, 12), D("7")),
    ]


def test_compose_without_overlays_is_the_baseline():
    points = [(datetime.date(2026, 5, 10), D("10"))]
    assert pps.compose(points, []) == points


# ── fences ───────────────────────────────────────────────────────────────────

async def test_toggle_off_excludes_the_funding_series_and_its_pending_rows(db_session):
    books = await _books(db_session)
    rid = await _transfer(db_session, books["org_id"], books["main"], books["savings"])
    # The May occurrence is already generated as a pending pair.
    await recurring_service.generate_due_transactions(
        db_session, books["org_id"], today=datetime.date(2026, 5, 20))
    plan = _plan(books["org_id"])
    on = await _project(db_session, books, [plan], [_goal(
        books["org_id"], monthly=D("100.00"), account_id=books["savings"], funding=rid)])
    off = await _project(db_session, books, [plan], [_goal(
        books["org_id"], monthly=D("100.00"), account_id=books["savings"], funding=rid,
        enabled=False)])
    # Committed and fully funded: the overlay is zero, so ON is the books.
    assert on.with_plans == on.actual
    # OFF drops the series, May's pending pair included: 8 occurrences May..Dec.
    assert off.with_plans[-1][1] - on.with_plans[-1][1] == D("800.00")
    assert off.actual == on.actual


async def test_a_yearly_series_counts_only_in_its_month(db_session):
    books = await _books(db_session)
    db_session.add(_series(books["org_id"], books["main"], books["trip"], "300.00",
                           datetime.date(2026, 8, 5), frequency="yearly"))
    await db_session.commit()
    result = await _project(db_session, books, [_plan(
        books["org_id"], budget=D("500.00"), category_id=books["trip"])], [])
    by_month = {o.month: o.amount for o in result.overlays if o.goal_id is None}
    assert by_month[datetime.date(2026, 8, 1)] == D("200.00")
    assert by_month[datetime.date(2026, 7, 1)] == D("500.00")
    assert by_month[datetime.date(2026, 9, 1)] == D("500.00")


async def test_funding_above_the_budget_is_flagged_never_a_negative_overlay(db_session):
    books = await _books(db_session)
    db_session.add(_series(books["org_id"], books["main"], books["trip"], "700.00",
                           datetime.date(2026, 6, 5)))
    await db_session.commit()
    result = await _project(db_session, books, [_plan(
        books["org_id"], budget=D("500.00"), category_id=books["trip"])], [])
    june = datetime.date(2026, 6, 1)
    assert all(o.amount > 0 for o in result.overlays)
    assert june not in {o.month for o in result.overlays}
    assert (1, None, june, D("200.00")) in {
        (f.plan_id, f.goal_id, f.month, f.excess) for f in result.over_funded}
    # Allocated is the budget; the plan's own spend is added back to the surplus.
    m = _month(result, june)
    assert m.allocated == D("500.00")
    assert m.surplus_ex_plans == m.free_pool_net + D("700.00")


async def test_no_path_sums_two_currencies(db_session):
    books = await _books(db_session)
    db_session.add(_series(books["org_id"], books["usd"], books["food"], "5000.00",
                           datetime.date(2026, 6, 5)))
    await db_session.commit()
    result = await _project(db_session, books, [_plan(books["org_id"], budget=D("50.00"))], [])
    assert result.currency == "EUR"
    # EUR main alone: 1000 opening, then 50 a month of plan budget.
    assert result.actual[0][1] == D("1000.00")
    assert result.with_plans[-1][1] == D("1000.00") - 8 * D("50.00")
    assert all(m.free_pool_net == 0 for m in result.months)


async def test_a_goal_account_transfer_reduces_the_free_pool_surplus(db_session):
    books = await _books(db_session)
    rid = await _transfer(db_session, books["org_id"], books["main"], books["savings"],
                          amount="100.00", next_due=datetime.date(2026, 6, 20))
    result = await _project(db_session, books, [_plan(books["org_id"])], [_goal(
        books["org_id"], monthly=D("150.00"), account_id=books["savings"], funding=rid)])
    june = _month(result, datetime.date(2026, 6, 1))
    # Savings is a goal account, so the transfer leaves the free pool.
    assert june.free_pool_net == D("-100.00")
    assert june.surplus_ex_plans == D("0.00")
    assert june.allocated == D("150.00")
    assert june.under_over == D("-150.00")
    assert june.annualised == D("-1800.00")
    # The 50 shortfall is the overlay, moved from the free pool to the goal account.
    june_overlay = [o for o in result.overlays if o.month == datetime.date(2026, 6, 1)]
    assert [o.amount for o in june_overlay] == [D("50.00")]
    # May's overlay is the whole 150 (nothing funded yet), June's 50, plus June's transfer.
    savings = dict(result.goal_accounts[books["savings"]])
    assert savings[datetime.date(2026, 6, 30)] == D("150.00") + D("50.00") + D("100.00")


async def test_a_lump_goal_lands_on_its_target_date(db_session):
    books = await _books(db_session)
    result = await _project(db_session, books, [_plan(books["org_id"])], [_goal(
        books["org_id"], kind="lump", target_amount=D("400.00"),
        target_date=datetime.date(2026, 9, 15))])
    points = dict(result.with_plans)
    assert points[datetime.date(2026, 9, 14)] == D("1000.00")
    assert points[datetime.date(2026, 9, 15)] == D("600.00")


async def test_retirement_goals_wait_for_the_lifetime_tier(db_session):
    books = await _books(db_session)
    result = await _project(db_session, books, [_plan(books["org_id"])], [_goal(
        books["org_id"], kind="retirement", monthly=D("100.00"))])
    assert result.overlays == []
    assert result.with_plans == result.actual


async def test_card_spend_is_not_added_back_to_the_free_pool(db_session):
    """A plan expense charged to a card leaves the free pool only when the card is paid."""
    books = await _books(db_session)
    db_session.add(_series(books["org_id"], books["card"], books["trip"], "100.00",
                           datetime.date(2026, 6, 5)))
    await db_session.commit()
    result = await _project(db_session, books, [_plan(
        books["org_id"], budget=D("100.00"), category_id=books["trip"])], [])
    june = _month(result, datetime.date(2026, 6, 1))
    assert june.surplus_ex_plans == june.free_pool_net


# ── review fences (#1098) ────────────────────────────────────────────────────

JUNE = datetime.date(2026, 6, 1)
MAY = datetime.date(2026, 5, 1)


async def test_spend_in_another_currency_never_funds_the_plan(db_session):
    books = await _books(db_session)
    db_session.add(_series(books["org_id"], books["usd"], books["trip"], "500.00",
                           datetime.date(2026, 6, 5)))
    await db_session.commit()
    result = await _project(db_session, books, [_plan(
        books["org_id"], budget=D("500.00"), category_id=books["trip"])], [])
    assert {o.month: o.amount for o in result.overlays}[JUNE] == D("500.00")
    assert _month(result, JUNE).surplus_ex_plans == _month(result, JUNE).free_pool_net


async def test_one_series_funds_one_allocation(db_session):
    """Two plans on one category, and a goal funded by a plan-category series:
    the books' 100 is credited once, so the overlays make up the rest."""
    books = await _books(db_session)
    s = _series(books["org_id"], books["main"], books["trip"], "100.00", datetime.date(2026, 6, 5))
    db_session.add(s)
    await db_session.commit()
    two = await _project(db_session, books, [
        _plan(books["org_id"], pid=1, budget=D("100.00"), category_id=books["trip"]),
        _plan(books["org_id"], pid=2, budget=D("100.00"), category_id=books["trip"]),
    ], [])
    goal = await _project(db_session, books, [_plan(
        books["org_id"], budget=D("100.00"), category_id=books["trip"])], [_goal(
        books["org_id"], kind="monthly_spend", monthly=D("100.00"), funding=s.id)])
    for result in (two, goal):
        june = _month(result, JUNE)
        overlaid = sum((o.amount for o in result.overlays if o.month == JUNE), D("0"))
        assert overlaid + D("100.00") == june.allocated == D("200.00")
        assert june.surplus_ex_plans == june.free_pool_net + D("100.00")


async def test_an_exhausted_series_last_pending_row_still_funds_its_month(db_session):
    books = await _books(db_session)
    s = _series(books["org_id"], books["main"], books["trip"], "100.00", datetime.date(2026, 5, 20))
    s.occurrence_count = 1
    db_session.add(s)
    await db_session.commit()
    await recurring_service.generate_due_transactions(
        db_session, books["org_id"], today=datetime.date(2026, 5, 20))
    result = await _project(db_session, books, [_plan(
        books["org_id"], budget=D("100.00"), category_id=books["trip"])], [])
    assert MAY not in {o.month for o in result.overlays}
    assert _month(result, MAY).surplus_ex_plans == _month(result, MAY).free_pool_net + D("100.00")


async def test_the_default_window_keeps_an_overdue_pending_row(db_session):
    books = await _books(db_session)
    db_session.add(_series(books["org_id"], books["main"], books["trip"], "100.00",
                           datetime.date(2026, 5, 5)))
    await db_session.commit()
    await recurring_service.generate_due_transactions(
        db_session, books["org_id"], today=datetime.date(2026, 5, 5))
    result = await pps.project(db_session, books["org_id"], [_plan(
        books["org_id"], budget=D("100.00"), category_id=books["trip"])], [],
        today=TODAY, horizon_end=HORIZON)
    assert MAY not in {o.month for o in result.overlays}


async def test_a_future_dated_settled_row_funds_its_month(db_session):
    books = await _books(db_session)
    s = _series(books["org_id"], books["main"], books["trip"], "100.00", datetime.date(2026, 6, 25))
    db_session.add(s)
    await db_session.flush()
    db_session.add(Transaction(
        org_id=books["org_id"], account_id=books["main"], category_id=books["trip"],
        amount=D("100.00"), type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
        date=datetime.date(2026, 5, 25), settled_date=datetime.date(2026, 5, 25),
        description="x", is_imported=False, is_manual_adjustment=False, recurring_id=s.id,
    ))
    await db_session.commit()
    result = await _project(db_session, books, [_plan(
        books["org_id"], budget=D("100.00"), category_id=books["trip"])], [])
    assert MAY not in {o.month for o in result.overlays}


async def test_allocation_stops_after_the_target_date(db_session):
    books = await _books(db_session)
    result = await _project(db_session, books, [_plan(books["org_id"])], [_goal(
        books["org_id"], monthly=D("100.00"), account_id=books["savings"],
        target_date=datetime.date(2026, 7, 31))])
    assert _month(result, datetime.date(2026, 7, 1)).allocated == D("100.00")
    assert _month(result, datetime.date(2026, 8, 1)).allocated == D("0")


async def test_a_funded_lump_is_spent_from_its_account_and_its_funding_is_not_added_back(db_session):
    books = await _books(db_session)
    rid = await _transfer(db_session, books["org_id"], books["main"], books["savings"],
                          amount="100.00", next_due=datetime.date(2026, 6, 20))
    result = await _project(db_session, books, [_plan(books["org_id"])], [_goal(
        books["org_id"], kind="lump", target_amount=D("400.00"),
        target_date=datetime.date(2026, 9, 30), account_id=books["savings"], funding=rid)])
    sep30 = datetime.date(2026, 9, 30)
    # Four transfers Jun..Sep leave the pool; the lump leaves the savings account.
    assert dict(result.with_plans)[sep30] == D("600.00")
    assert dict(result.goal_accounts[books["savings"]])[sep30] == D("0.00")
    june = _month(result, JUNE)
    assert june.under_over == june.free_pool_net == D("-100.00")
