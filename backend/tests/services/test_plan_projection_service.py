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
from app.models.account import PaymentStrategy
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


# ── card-charged plan spend (TBD-793) ────────────────────────────────────────

async def _paid_card(db, books, *, strategy=PaymentStrategy.FULL_BALANCE, fixed=None,
                     close=10, pay=5, relative=1):
    """The books' card, paid from Main: by default it closes on the 10th and is
    paid on the 5th of the next month."""
    card = await db.get(Account, books["card"])
    card.close_day, card.payment_day, card.payment_day_relative_month = close, pay, relative
    card.payment_source_account_id = books["main"]
    card.payment_strategy, card.fixed_payment_amount = strategy, fixed
    await db.commit()


async def _plan_and_food_on(db, books, account_id):
    """The plan's series (100) and an unrelated one (50), both on the 15th."""
    trip = _series(books["org_id"], account_id, books["trip"], "100.00", datetime.date(2026, 6, 15))
    food = _series(books["org_id"], account_id, books["food"], "50.00", datetime.date(2026, 6, 15))
    db.add_all([trip, food])
    await db.commit()
    return [trip, food]


async def test_card_charged_plan_spend_reconciles_like_pool_spend(db_session):
    """FENCE (TBD-793). A plan series charged to a card the free pool pays
    leaves ``under_over`` equal, month by month, to the same series charged to
    a free-pool account. Only the plan's share of the payment is added back,
    in the month the payment leaves the pool. Wrong implementations: no
    add-back (the payment month is over-allocated by 100), adding back the
    whole payment (the food spend vanishes), or adding back at charge time.
    The unrelated series stays on the card in both: it shares the payment."""
    books = await _books(db_session)
    await _paid_card(db_session, books)
    series = await _plan_and_food_on(db_session, books, books["card"])
    plan = [_plan(books["org_id"], budget=D("100.00"), category_id=books["trip"])]
    on_card = await _project(db_session, books, plan, [])
    series[0].account_id = books["main"]  # only the plan's series moves
    await db_session.commit()
    on_pool = await _project(db_session, books, plan, [])

    assert [m.under_over for m in on_card.months] == [m.under_over for m in on_pool.months]
    # The card really moves the outflow: June's spend leaves Main on Aug 5.
    june, aug = JUNE, datetime.date(2026, 8, 1)
    assert (_month(on_card, june).free_pool_net, _month(on_pool, june).free_pool_net) == (
        D("0.00"), D("-100.00"))
    assert _month(on_card, aug).free_pool_net == D("-150.00")
    assert _month(on_card, aug).surplus_ex_plans == D("-50.00")


async def test_a_partial_card_payment_settles_plan_spend_first(db_session):
    """A fixed 30 a month pays less than the plan charges. The whole payment
    is plan spend (plan first), so the surplus is untouched by it."""
    books = await _books(db_session)
    await _paid_card(db_session, books, strategy=PaymentStrategy.FIXED_AMOUNT, fixed=D("30.00"))
    await _plan_and_food_on(db_session, books, books["card"])
    result = await _project(db_session, books, [_plan(
        books["org_id"], budget=D("100.00"), category_id=books["trip"])], [])
    aug = _month(result, datetime.date(2026, 8, 1))
    assert (aug.free_pool_net, aug.surplus_ex_plans) == (D("-30.00"), D("0.00"))


async def _reconciles_like_pool(db, books, moved):
    """Project with ``moved`` on the card, then with it on Main; return both."""
    plan = [_plan(books["org_id"], budget=D("100.00"), category_id=books["trip"])]
    on_card = await _project(db, books, plan, [])
    for obj in moved:
        obj.account_id = books["main"]
    await db.commit()
    on_pool = await _project(db, books, plan, [])
    assert [m.under_over for m in on_card.months] == [m.under_over for m in on_pool.months]
    return on_card, on_pool


async def test_a_charge_its_own_cycle_skips_is_billed_by_the_next(db_session):
    """FENCE (TBD-793 review). The card closes on the 3rd and is paid on the
    25th of the same month. The May 2 occurrence's cycle is paid on May 25,
    inside the first period, which bills real rows only. So June 25 pays May
    and June together (200), and both are added back then."""
    books = await _books(db_session)
    await _paid_card(db_session, books, close=3, pay=25, relative=0)
    trip = _series(books["org_id"], books["card"], books["trip"], "100.00", datetime.date(2026, 5, 2))
    db_session.add(trip)
    await db_session.commit()
    on_card, _ = await _reconciles_like_pool(db_session, books, [trip])
    june = _month(on_card, JUNE)
    assert (june.free_pool_net, june.surplus_ex_plans) == (D("-200.00"), D("0.00"))


async def test_a_plan_charge_settled_before_today_is_added_back_when_paid(db_session):
    """FENCE (TBD-793 review). A plan row settled on the card on May 5, before
    today (May 10), is paid on June 5. On Main it would sit in the seed; on the
    card its payment lowers June, so June adds it back."""
    books = await _books(db_session)
    await _paid_card(db_session, books)
    trip = _series(books["org_id"], books["card"], books["trip"], "100.00", datetime.date(2026, 6, 15))
    db_session.add(trip)
    await db_session.flush()
    row = Transaction(
        org_id=books["org_id"], account_id=books["card"], category_id=books["trip"],
        amount=D("100.00"), type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
        date=datetime.date(2026, 5, 5), settled_date=datetime.date(2026, 5, 5),
        description="x", is_imported=False, is_manual_adjustment=False, recurring_id=trip.id,
    )
    db_session.add(row)
    await db_session.commit()
    on_card, _ = await _reconciles_like_pool(db_session, books, [trip, row])
    june = _month(on_card, JUNE)
    assert (june.free_pool_net, june.surplus_ex_plans) == (D("-100.00"), D("0.00"))


async def test_a_plan_charge_paid_for_real_is_never_added_back_later(db_session):
    """FENCE (TBD-793 review). The card closes on the 3rd and is paid on the
    15th. An Apr 20 plan charge was really paid on May 15 by a linked transfer
    from Main (a credit the kernel nets against that cycle), so the walk's
    payments cover only the new trip (100) and food (50) charges: every month
    from June keeps ``surplus_ex_plans`` at -50. Wrong implementation: credits
    not counted as settling plan spend, so a later payment re-attributes the
    paid Apr 20 charge."""
    books = await _books(db_session)
    await _paid_card(db_session, books, close=3, pay=15, relative=0)
    trip = _series(books["org_id"], books["card"], books["trip"], "100.00", datetime.date(2026, 5, 25))
    food = _series(books["org_id"], books["card"], books["food"], "50.00", datetime.date(2026, 5, 25))
    db_session.add_all([trip, food])
    await db_session.flush()

    def row(account_id, tx_type, day, *, rid=None, cat=books["food"]):
        return Transaction(
            org_id=books["org_id"], account_id=account_id, category_id=cat, amount=D("100.00"),
            type=tx_type, status=TransactionStatus.SETTLED, date=day, settled_date=day,
            description="x", is_imported=False, is_manual_adjustment=False, recurring_id=rid)

    db_session.add(row(books["card"], TransactionType.EXPENSE, datetime.date(2026, 4, 20),
                       rid=trip.id, cat=books["trip"]))
    out = row(books["main"], TransactionType.EXPENSE, datetime.date(2026, 5, 15))
    into = row(books["card"], TransactionType.INCOME, datetime.date(2026, 5, 15))
    db_session.add_all([out, into])
    await db_session.flush()
    out.linked_transaction_id, into.linked_transaction_id = into.id, out.id
    await db_session.commit()
    result = await pps.project(
        db_session, books["org_id"], [_plan(books["org_id"], budget=D("100.00"),
                                            category_id=books["trip"])], [],
        today=datetime.date(2026, 5, 20), horizon_end=HORIZON, window_start=P_START)
    assert {m.surplus_ex_plans for m in result.months if m.month >= JUNE} == {D("-50.00")}


async def test_a_plan_charge_paid_before_its_close_is_never_added_back_later(db_session):
    """FENCE (TBD-793 review). The card closes on the 10th and is paid on the
    5th of the next month. An Apr 12 plan charge was really paid on May 8,
    before the May 10 close, so the kernel bills nothing on Jun 5 and only the
    new trip (100) and food (50) charges after that: every month from July
    keeps ``surplus_ex_plans`` at -50. Wrong implementation: plan charges not
    capped by what the card still owed at close, so the Jul 5 payment counts
    the food spend as plan spend."""
    books = await _books(db_session)
    await _paid_card(db_session, books)
    trip = _series(books["org_id"], books["card"], books["trip"], "100.00", datetime.date(2026, 5, 15))
    food = _series(books["org_id"], books["card"], books["food"], "50.00", datetime.date(2026, 5, 15))
    db_session.add_all([trip, food])
    await db_session.flush()

    def row(account_id, tx_type, day, *, rid=None, cat=books["food"]):
        return Transaction(
            org_id=books["org_id"], account_id=account_id, category_id=cat, amount=D("100.00"),
            type=tx_type, status=TransactionStatus.SETTLED, date=day, settled_date=day,
            description="x", is_imported=False, is_manual_adjustment=False, recurring_id=rid)

    db_session.add(row(books["card"], TransactionType.EXPENSE, datetime.date(2026, 4, 12),
                       rid=trip.id, cat=books["trip"]))
    out = row(books["main"], TransactionType.EXPENSE, datetime.date(2026, 5, 8))
    into = row(books["card"], TransactionType.INCOME, datetime.date(2026, 5, 8))
    db_session.add_all([out, into])
    await db_session.flush()
    out.linked_transaction_id, into.linked_transaction_id = into.id, out.id
    await db_session.commit()
    result = await pps.project(
        db_session, books["org_id"], [_plan(books["org_id"], budget=D("100.00"),
                                            category_id=books["trip"])], [],
        today=datetime.date(2026, 5, 20), horizon_end=HORIZON, window_start=P_START)
    july = datetime.date(2026, 7, 1)
    assert {m.surplus_ex_plans for m in result.months if m.month >= july} == {D("-50.00")}


async def test_a_plan_charge_from_the_previous_statement_is_not_read(db_session):
    """FENCE (TBD-793 review). The card closes on the 31st (Apr 30 in April)
    and is paid on the 5th. A Mar 31 plan charge belongs to the statement
    that closed on Mar 31, and was paid on Apr 3. The May 5 payment covers
    only the Apr 15 food charge (150), so May adds nothing back. Wrong
    implementation: reading rows from "a month before the close" (Mar 30),
    which counts the paid Mar 31 charge as owed on the Apr 30 statement."""
    books = await _books(db_session)
    await _paid_card(db_session, books, close=31, pay=5, relative=1)
    trip = _series(books["org_id"], books["card"], books["trip"], "100.00", datetime.date(2026, 7, 1))
    db_session.add(trip)
    await db_session.flush()

    def row(account_id, tx_type, day, amount, *, rid=None, cat=books["food"]):
        return Transaction(
            org_id=books["org_id"], account_id=account_id, category_id=cat, amount=D(amount),
            type=tx_type, status=TransactionStatus.SETTLED, date=day, settled_date=day,
            description="x", is_imported=False, is_manual_adjustment=False, recurring_id=rid)

    db_session.add_all([
        row(books["card"], TransactionType.EXPENSE, datetime.date(2026, 3, 31), "100.00",
            rid=trip.id, cat=books["trip"]),
        row(books["card"], TransactionType.EXPENSE, datetime.date(2026, 4, 15), "150.00"),
    ])
    out = row(books["main"], TransactionType.EXPENSE, datetime.date(2026, 4, 3), "100.00")
    into = row(books["card"], TransactionType.INCOME, datetime.date(2026, 4, 3), "100.00")
    db_session.add_all([out, into])
    await db_session.flush()
    out.linked_transaction_id, into.linked_transaction_id = into.id, out.id
    await db_session.commit()
    result = await pps.project(
        db_session, books["org_id"], [_plan(books["org_id"], budget=D("100.00"),
                                            category_id=books["trip"])], [],
        today=datetime.date(2026, 5, 1), horizon_end=HORIZON, window_start=P_START)
    may = _month(result, MAY)
    assert (may.free_pool_net, may.surplus_ex_plans) == (D("-150.00"), D("-150.00"))
