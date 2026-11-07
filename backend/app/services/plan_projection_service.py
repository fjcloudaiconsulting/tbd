"""Plans over the shared baseline (TBD-562): goal overlays, per-currency
reconciliation, and goal toggles that re-project.

* **Actual** is ``projection_kernel.baseline`` over the books, no overlays.
* **With plans** is the baseline less the funding series of every goal toggled
  OFF (their frontier AND their generated pending rows, via
  ``exclude_recurring_ids``), plus the overlays of the plans passed in.
* The curves are the **free pool**: active non-liability accounts in the plan
  currency that are no goal's account. Goal accounts are projected beside it.

A plan's funding in a month (F) is what the books already put towards it,
in the plan currency: the rows the kernel lays (pending, future-dated settled)
plus un-materialised occurrences of its series. F_save is a monthly goal's
``funding_recurring_id``; F_spend is the plan's expense series in its own
category. Each series counts against one allocation (goals first, then plans
by id). Overlays only top the books up to the target, so F above a target is
reported in ``over_funded`` and never becomes income:

* plan budget: ``max(0, monthly_budget - F_spend)`` out of the free pool;
* a goal with ``monthly_amount`` (savings target, monthly spend):
  ``max(0, monthly_amount - F_save)`` out of the free pool, and into the goal
  account for a savings target; until ``target_date`` when one is set;
* a lump goal: ``target_amount`` on ``target_date``, out of the goal's
  account when it has one (where it was saved), else out of the free pool;
* a retirement goal: none here (the lifetime tier, TBD-563).

A monthly overlay lands on the 1st of its month (month 0: the walk's first
day), the conservative side of a month's cash flow.

Reconciliation per currency, per calendar month of the walk:
``surplus_ex_plans = free-pool net + the claimed series' flows on free-pool
accounts``, ``allocated = budgets + goals' monthly amounts`` (until each
goal's ``target_date``),
``under_over = surplus_ex_plans - allocated`` (negative: over-allocated), and
``annualised = under_over x 12``. Month 0's net runs from the walk's opening
seed, so it covers the rest of the current month only.
"""

import datetime
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.recurring import RecurringTransaction
from app.models.scenario import PlanGoal, PlanGoalKind, Scenario
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.services import projection_kernel
from app.services.account_balance_forecast_service import LIABILITY_SLUGS
from app.services.projection_kernel import Point, _recurring_legs
from app.services.transaction_filters import (
    balance_contribution_filter,
    effective_period_date_expr,
    non_reverted_transaction_filter,
)

_ZERO = Decimal("0")
_MONTHLY_KINDS = {PlanGoalKind.SAVINGS_TARGET.value, PlanGoalKind.MONTHLY_SPEND.value}


@dataclass(frozen=True)
class Overlay:
    plan_id: int
    goal_id: int | None  # None: the plan's own budget
    month: datetime.date  # the 1st of the month it tops up
    on: datetime.date  # the day it lands
    amount: Decimal  # positive
    goal_account_id: int | None = None  # into this account (out of it when not from_pool)
    from_pool: bool = True


@dataclass(frozen=True)
class OverFunding:
    plan_id: int
    goal_id: int | None
    month: datetime.date
    excess: Decimal


@dataclass(frozen=True)
class MonthReconciliation:
    month: datetime.date
    free_pool_net: Decimal
    surplus_ex_plans: Decimal
    allocated: Decimal
    under_over: Decimal
    annualised: Decimal


@dataclass
class PlanProjection:
    currency: str
    actual: list[Point]
    with_plans: list[Point]
    goal_accounts: dict[int, list[Point]]
    months: list[MonthReconciliation]
    overlays: list[Overlay] = field(default_factory=list)
    over_funded: list[OverFunding] = field(default_factory=list)


def compose(points: Sequence[Point], deltas: Iterable[Point]) -> list[Point]:
    """``points`` plus every delta from its day on. A delta dated before the
    walk lands on day 0; one after the last point is outside the projection."""
    pending = sorted(deltas)
    out: list[Point] = []
    running, i = _ZERO, 0
    for day, balance in points:
        while i < len(pending) and pending[i][0] <= day:
            running += pending[i][1]
            i += 1
        out.append((day, balance + running))
    return out


def _month_of(day: datetime.date) -> datetime.date:
    return day.replace(day=1)


def _month_ends(points: Sequence[Point]) -> dict[datetime.date, Decimal]:
    """Calendar month -> its last point's balance."""
    return {_month_of(day): balance for day, balance in points}


def _sum_points(series: Iterable[Sequence[Point]], dates: Sequence[datetime.date]) -> list[Point]:
    totals = [_ZERO] * len(dates)
    for points in series:
        totals = [t + b for t, (_, b) in zip(totals, points)]
    return list(zip(dates, totals))


async def project(
    db: AsyncSession,
    org_id: int,
    plans: Sequence[Scenario],
    goals: Sequence[PlanGoal],
    *,
    today: datetime.date,
    horizon_end: datetime.date,
    window_start: datetime.date | None = None,
) -> PlanProjection:
    """Project ``plans`` (one currency: a plan is always in the org's) and
    their ``goals`` over the org's books up to ``horizon_end``."""
    currencies = {p.currency for p in plans}
    if len(currencies) != 1:
        raise ValueError("plans must share one currency")
    (currency,) = currencies
    plan_ids = {p.id for p in plans}
    goals = [g for g in goals if g.scenario_id in plan_ids]
    enabled = [g for g in goals if g.enabled]
    excluded = sorted({g.funding_recurring_id for g in goals
                       if not g.enabled and g.funding_recurring_id is not None})

    kw = {"today": today, "horizon_end": horizon_end, "window_start": window_start}
    books = await projection_kernel.baseline(db, org_id, **kw)
    base = (await projection_kernel.baseline(db, org_id, exclude_recurring_ids=excluded, **kw)
            if excluded else books)
    dates = [day for day, _ in base.accounts[0].points] if base.accounts else [base.walk_start]
    months = sorted({_month_of(d) for d in dates})

    goal_account_ids = {g.account_id for g in goals if g.account_id is not None}
    in_currency = {a.account.id for a in base.accounts if a.account.currency == currency}
    pool = {a.account.id for a in base.accounts
            if a.account.id in in_currency and a.type_slug not in LIABILITY_SLUGS
            and a.account.id not in goal_account_ids}

    # The candidate series: each plan's expense series in its category, each
    # enabled goal's funding series, in the plan currency. No activity filter:
    # an exhausted series projects nothing, but its pending rows still land.
    # Toggled-off series are out of ``base`` and out of here.
    category_ids = {p.category_id for p in plans if p.category_id is not None}
    funding_ids = {g.funding_recurring_id for g in enabled if g.funding_recurring_id is not None}
    series = [r for r in (await db.execute(select(RecurringTransaction).where(
        RecurringTransaction.org_id == org_id,
        *([RecurringTransaction.id.not_in(excluded)] if excluded else []),
        (RecurringTransaction.id.in_(funding_ids))
        | (RecurringTransaction.category_id.in_(category_ids)
           & (RecurringTransaction.type == "expense")),
    ))).scalars().all() if r.account_id in in_currency] if funding_ids or category_ids else []

    # Each series counts against ONE allocation: a monthly goal's own funding
    # series first, then the plans' category series, by plan id. Only claimed
    # series are added back to the surplus; an unclaimed one (a lump's
    # funding, a plan with no budget) is ordinary spending.
    claimed: set[int] = set()
    goal_series: dict[int, set[int]] = {}
    for g in enabled:
        if g.kind in _MONTHLY_KINDS and g.monthly_amount is not None:
            goal_series[g.id] = {r.id for r in series if r.id == g.funding_recurring_id} - claimed
            claimed |= goal_series[g.id]
    plan_series: dict[int, set[int]] = {}
    for p in sorted(plans, key=lambda p: p.id):
        if p.monthly_budget is not None:
            plan_series[p.id] = {r.id for r in series if r.type == "expense"
                                 and r.category_id == p.category_id} - claimed
            claimed |= plan_series[p.id]

    # Per series and month: F (its magnitude) and its signed free-pool effect.
    funded: dict[tuple[int, datetime.date], Decimal] = {}
    pool_effect: dict[datetime.date, Decimal] = {}

    def book(rid, account_id, primary, day, delta):
        month = _month_of(max(day, base.walk_start))
        if primary:
            funded[rid, month] = funded.get((rid, month), _ZERO) + abs(delta)
        # shortcut: plan spend charged to a card is never added back, so the
        # card payment that settles it understates under/over; upgrade when card
        # payments carry the charges they settle.
        if rid in claimed and account_id in pool:
            pool_effect[month] = pool_effect.get(month, _ZERO) + delta

    for r in series:
        for day in base.series_dates.get(r.id, []):
            for account_id, delta in _recurring_legs(r):
                book(r.id, account_id, account_id == r.account_id, day, delta)
    if series:
        # The rows the kernel lays on the walk: pending ones in the window, and
        # future-dated settled ones inside the first period.
        eff = effective_period_date_expr()
        rows = await db.execute(select(
            Transaction.recurring_id, Transaction.account_id, Transaction.type,
            Transaction.amount, eff,
        ).where(
            Transaction.org_id == org_id,
            Transaction.recurring_id.in_([r.id for r in series]),
            or_(
                and_(
                    Transaction.status == TransactionStatus.PENDING,
                    Transaction.is_manual_adjustment.is_(False),
                    non_reverted_transaction_filter(),
                    eff >= base.window_start, eff <= horizon_end,
                ),
                and_(
                    Transaction.status == TransactionStatus.SETTLED,
                    balance_contribution_filter(),
                    eff > base.walk_start, eff <= min(base.first_period_end, horizon_end),
                ),
            ),
        ))
        source = {r.id: r.account_id for r in series}
        for rid, account_id, tx_type, amount, day in rows.all():
            delta = Decimal(str(amount))
            if tx_type == TransactionType.EXPENSE:
                delta = -delta
            book(rid, account_id, account_id == source[rid],
                 projection_kernel._as_date(day), delta)

    def f(series_ids, month):
        return sum((funded.get((rid, month), _ZERO) for rid in series_ids), _ZERO)

    overlays: list[Overlay] = []
    over_funded: list[OverFunding] = []
    allocated = dict.fromkeys(months, _ZERO)

    def top_up(plan_id, goal_id, target, series_ids, until, goal_account_id=None):
        for month in months:
            if until is not None and month > until:
                break
            allocated[month] += target
            gap = target - f(series_ids, month)
            if gap > 0:
                overlays.append(Overlay(plan_id, goal_id, month, max(month, base.walk_start),
                                        gap, goal_account_id))
            elif gap < 0:
                over_funded.append(OverFunding(plan_id, goal_id, month, -gap))

    for p in plans:
        if p.monthly_budget is not None:
            top_up(p.id, None, p.monthly_budget, plan_series[p.id], None)
    for g in enabled:
        if g.id in goal_series:
            into = g.account_id if g.kind == PlanGoalKind.SAVINGS_TARGET.value else None
            top_up(g.scenario_id, g.id, g.monthly_amount, goal_series[g.id], g.target_date, into)
        elif g.kind == PlanGoalKind.LUMP.value and g.target_amount and g.target_date:
            # Spent from the goal's account when it has one: that is where it was saved.
            overlays.append(Overlay(g.scenario_id, g.id, _month_of(g.target_date),
                                    g.target_date, g.target_amount, g.account_id,
                                    from_pool=g.account_id is None))

    def pool_points(kb):
        return _sum_points((a.points for a in kb.accounts if a.account.id in pool), dates)

    with_plans = compose(pool_points(base), [(o.on, -o.amount) for o in overlays if o.from_pool])
    goal_accounts = {
        a.account.id: compose(a.points, [(o.on, o.amount if o.from_pool else -o.amount)
                                         for o in overlays if o.goal_account_id == a.account.id])
        for a in base.accounts if a.account.id in goal_account_ids
    }

    ends = _month_ends(pool_points(base))
    previous = sum((a.seed for a in base.accounts if a.account.id in pool), _ZERO)
    reconciliation = []
    for month in months:
        net = ends[month] - previous
        previous = ends[month]
        # Adding back the claimed series' flows on the free pool: an outflow is negative.
        surplus = net - pool_effect.get(month, _ZERO)
        under_over = surplus - allocated[month]
        reconciliation.append(MonthReconciliation(
            month, net, surplus, allocated[month], under_over, under_over * 12))

    return PlanProjection(
        currency=currency,
        actual=pool_points(books),
        with_plans=with_plans,
        goal_accounts=goal_accounts,
        months=reconciliation,
        overlays=overlays,
        over_funded=over_funded,
    )
