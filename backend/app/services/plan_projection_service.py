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
accounts`` (a claimed charge on a card the free pool pays counts in the month
its cycle's payment leaves the pool, up to that payment; TBD-793),
``allocated = budgets + goals' monthly amounts`` (until each
goal's ``target_date``),
``under_over = surplus_ex_plans - allocated`` (negative: over-allocated), and
``annualised = under_over x 12``. Month 0's net runs from the walk's opening
seed, so it covers the rest of the current month only.
"""

import datetime
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from dateutil.relativedelta import relativedelta
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.recurring import RecurringTransaction
from app.models.scenario import PlanGoal, PlanGoalKind, Scenario
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.services import projection_kernel
from app.services.account_balance_forecast_service import LIABILITY_SLUGS
from app.services.date_utils import occurrences_in_window
from app.services.projection_kernel import _CENTS, Point, _recurring_legs
from app.services.recurring_filters import active_series_filter, remaining_occurrences
from app.services.transaction_filters import (
    balance_contribution_filter,
    effective_period_date_expr,
    non_reverted_transaction_filter,
    signed_amount_expr,
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
    # What the projection was built from, for ``lifetime``.
    baseline: projection_kernel.Baseline | None = None
    pool: frozenset[int] = frozenset()


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
    # Cards the free pool pays (the kernel synthesises their payments), and the
    # claimed charges on them: card -> [(day, amount, is a projected occurrence)].
    pool_paid_cards = {a.account.id: a for a in base.accounts
                       if a.type_slug == "credit_card" and a.account.id in in_currency
                       and a.account.close_day is not None
                       and a.account.payment_source_account_id in pool}
    card_charges: dict[int, list[tuple[datetime.date, Decimal, bool]]] = {}

    def book(rid, account_id, primary, day, delta, projected=False):
        month = _month_of(max(day, base.walk_start))
        if primary:
            funded[rid, month] = funded.get((rid, month), _ZERO) + abs(delta)
        if rid in claimed and account_id in pool:
            pool_effect[month] = pool_effect.get(month, _ZERO) + delta
        elif projected and rid in claimed and account_id in pool_paid_cards and delta < 0:
            # Real card rows come from the card's ledger below.
            card_charges.setdefault(account_id, []).append((day, -delta, True))

    for r in series:
        for day in base.series_dates.get(r.id, []):
            for account_id, delta in _recurring_legs(r):
                book(r.id, account_id, account_id == r.account_id, day, delta, projected=True)
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

    # A claimed card charge leaves the free pool with the payment that settles it
    # (TBD-793), so it is added back in that payment's month. Each due cycle is
    # read as the kernel's synthesis billed it (``AccountBaseline.cc_cycles``):
    # claimed charges up to its close, less those already settled, are paid
    # first, but never beyond what the card still owed at close (a real payment,
    # a refund or a credit before the close already paid the rest), then by the
    # cycle's payment-in credits (never added back), then by its projected
    # payment (added back). A charge a cycle did not bill (a projected
    # occurrence before the first period ends) is carried to the next one.
    paid_cards = {cid: c for cid, c in pool_paid_cards.items() if c.cc_cycles}
    claimed_on_cards = [r.id for r in series if r.id in claimed]
    if paid_cards and claimed_on_cards:
        eff = effective_period_date_expr()
        since = {cid: c.cc_cycles[0].period_start for cid, c in paid_cards.items()}
        # The card ledger's rows (``load_cc_ledgers``), for the claimed series,
        # from the first statement the walk pays: an earlier row is the previous
        # statement's, and reading it would re-attribute plan spend already paid.
        rows = await db.execute(select(Transaction.account_id, eff, signed_amount_expr()).where(
            Transaction.org_id == org_id,
            Transaction.account_id.in_(list(paid_cards)),
            Transaction.recurring_id.in_(claimed_on_cards),
            balance_contribution_filter(),
            eff >= min(since.values()),
        ))
        for card_id, day, amount in rows.all():
            day, amount = projection_kernel._as_date(day), Decimal(str(amount))
            if amount < 0 and day >= since[card_id]:
                card_charges.setdefault(card_id, []).append((day, -amount, False))
    for card_id, charges in card_charges.items():
        settled = _ZERO
        for bill in paid_cards[card_id].cc_cycles if card_id in paid_cards else []:
            billed_projected = bill.payment_date > base.first_period_end
            open_ = max(_ZERO, sum((amount for day, amount, projected in charges
                                    if day <= bill.close
                                    and (billed_projected or not projected)), _ZERO) - settled)
            unpaid = min(open_, bill.owed)
            by_credit = min(unpaid, bill.credits)
            # shortcut: a partial payment settles plan spend before other charges,
            # plan spend still unpaid from before the first statement is not
            # attributed, and plan spend a credit settles (a real payment, a
            # recurring transfer in) is never added back; upgrade to a pro-rata
            # share, attribute carried debt, and add back credits that leave the
            # free pool inside the walk, if those cards need it.
            share = min(unpaid - by_credit, bill.outflow)
            settled += open_ - unpaid + by_credit + share
            if share:
                month = _month_of(max(bill.payment_date, base.walk_start))
                pool_effect[month] = pool_effect.get(month, _ZERO) - share

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
        baseline=base,
        pool=frozenset(pool),
    )


# ── Lifetime tier (TBD-563) ──────────────────────────────────────────────────

DETAILED_MONTHS = 24
NO_BIRTH_YEAR_MONTHS = 480
MAX_LIFETIME_MONTHS = 1000


@dataclass
class GoalRequirement:
    goal_id: int
    # Extra monthly saving into the goal's account until its target date; None
    # when unreachable (no month left to save in and the target is short).
    monthly: Decimal | None


@dataclass
class Lifetime:
    """Monthly from the walk's month 0. ``ages`` is a display label only (None
    without a birth year: the date axis); the analysis domain is ``months``."""
    months: list[datetime.date]
    ages: list[int | None]
    nominal: list[Decimal]  # the free pool, compounded
    real: list[Decimal]  # deflated by inflation to month 0's money
    goal_accounts: dict[int, list[Decimal]]
    depletion_month: datetime.date | None
    depletion_age: int | None
    required: list[GoalRequirement]


def _lifetime_months(walk_start: datetime.date, birth_year: int | None, end_age: int) -> int:
    if birth_year is None:
        return NO_BIRTH_YEAR_MONTHS
    # Through December of the year they turn end_age; no date is built, so any year is safe.
    n = (birth_year + end_age - walk_start.year) * 12 + 12 - walk_start.month + 1
    return max(DETAILED_MONTHS, min(n, MAX_LIFETIME_MONTHS))


def _monthly_rate(pct: Decimal) -> Decimal:
    return Decimal(pct) / 100 / 12


def _grow(start: Decimal, flows: Sequence[Decimal], r: Decimal) -> list[Decimal]:
    """End-of-month balances: each month grows the last by ``r``, then adds its flow."""
    out, balance = [], start
    for flow in flows:
        balance = balance * (1 + r) + flow
        out.append(balance)
    return out


def _loan_payoff(owed: Decimal, payment: Decimal, apr: Decimal | None, months: int) -> list[Decimal]:
    """Payments, month by month, until ``owed`` is paid: interest first, the last one short."""
    r, out = _monthly_rate(apr or _ZERO), []
    while owed > 0 and len(out) < months:
        owed += owed * r
        pay = min(payment, owed)
        owed -= pay
        out.append(pay)
    return out


async def lifetime(
    db: AsyncSession,
    org_id: int,
    plans: Sequence[Scenario],
    goals: Sequence[PlanGoal],
    *,
    today: datetime.date,
    birth_year: int | None,
    return_pct: Decimal = _ZERO,
    inflation_pct: Decimal = _ZERO,
    end_age: int = 100,
) -> Lifetime:
    """The lifetime tier over the composed plans (one currency, like ``project``).

    Months 0..23 are the detailed walk's monthly nets, with plans. From month
    24 each bucket (the free pool, each goal account) moves by its months
    12..23 average net with its ENDING flows taken out, plus the ending flows
    still running that month: finite series to their last occurrence, loans
    to payoff, goal overlays to their target date, lumps in their month.
    Growth compounds from month 0, on non-liability balances only (the pool
    and goal accounts), so month 24 has no kink. Annual rates compound
    monthly at pct / 12, as ``scenario_engine._required_monthly_to_close_gap``. Retirement goals draw their
    ``monthly_amount`` (today's money, inflated) from their account from
    ``target_date``.
    """
    first = today.replace(day=1)
    horizon = first + relativedelta(months=DETAILED_MONTHS) - datetime.timedelta(days=1)
    proj = await project(db, org_id, plans, goals, today=today, horizon_end=horizon)
    base = proj.baseline
    n = _lifetime_months(base.walk_start, birth_year, end_age)
    months = [_month_of(base.walk_start) + relativedelta(months=m) for m in range(n)]
    index = {month: m for m, month in enumerate(months)}
    plan_ids = {p.id for p in plans}
    goals = [g for g in goals if g.scenario_id in plan_ids]
    enabled = [g for g in goals if g.enabled]
    by_id = {a.account.id: a for a in base.accounts}
    buckets = {None: proj.with_plans, **proj.goal_accounts}  # None: the free pool

    def at(month: datetime.date) -> int | None:
        return index.get(_month_of(month))

    ending: dict[int | None, list[Decimal]] = {b: [_ZERO] * n for b in buckets}

    def end_flow(account_id, month_index, delta):
        bucket = None if account_id in proj.pool else account_id
        if month_index is not None and bucket in ending:
            ending[bucket][month_index] += delta

    # Finite series: every occurrence to the last (the walk's own up to month
    # 23, ``occurrences_in_window`` past it). Toggled-off series are excluded.
    excluded = {g.funding_recurring_id for g in goals if not g.enabled}
    finite = (await db.execute(select(RecurringTransaction).where(
        RecurringTransaction.org_id == org_id,
        active_series_filter(),
        RecurringTransaction.occurrence_count.is_not(None),
    ))).scalars().all()
    cards = {a.account.id: a.account.payment_source_account_id
             for a in base.accounts if a.type_slug == "credit_card"}
    loans = {a.account.id: a for a in base.accounts if a.type_slug == "loan"}

    def book_walk_legs(r, dates):
        for d in dates:
            for account_id, delta in _recurring_legs(r):
                m = at(max(d, base.walk_start))
                if account_id in cards:
                    # shortcut: a card charge is paid from the card's source the
                    # month after; a charge past the close day waits one more.
                    # Upgrade with the kernel's cycle resolution.
                    account_id = cards[account_id]
                    m = m + 1 if m is not None and m + 1 < n else None
                end_flow(account_id, m, delta)

    # Transfers into a loan end at its payoff, with the scheduled payment.
    into_loan = (await db.execute(select(RecurringTransaction).where(
        RecurringTransaction.org_id == org_id,
        active_series_filter(),
        RecurringTransaction.type == "transfer",
        RecurringTransaction.to_account_id.in_(list(loans)),
    ))).scalars().all() if loans else []
    into_loan = [r for r in into_loan if r.id not in excluded]
    for r in finite:
        if r.id in excluded or r in into_loan:
            continue
        dates = list(base.series_dates.get(r.id, []))
        dates += [d for d in occurrences_in_window(
            r.next_due_date, r.frequency, horizon + datetime.timedelta(days=1),
            months[-1] + relativedelta(months=1) - datetime.timedelta(days=1),
            budget=remaining_occurrences(r),
        ) if d > horizon]
        book_walk_legs(r, dates)

    # Loans: the walk's payments (scheduled, and transfers into the loan) to
    # month 23, then payoff from the balance it leaves at the same monthly
    # payment and the loan's rate, shared among the payers.
    for loan_id, a in loans.items():
        transfers = [r for r in into_loan if r.to_account_id == loan_id]
        source = a.account.payment_source_account_id
        for d, amount in a.loan_payments:
            end_flow(source, at(d), -amount)
        for r in transfers:
            book_walk_legs(r, base.series_dates.get(r.id, []))
        # shortcut: a transfer counts as monthly whatever its frequency; upgrade
        # when non-monthly loan transfers matter.
        payers = [(r.account_id, Decimal(str(r.amount))) for r in transfers]
        if a.loan_payments and source is not None:
            payers.append((source, max(p for _, p in a.loan_payments)))
        payment = sum((p for _, p in payers), _ZERO)
        if payment <= 0:
            continue
        payoff = _loan_payoff(-a.points[-1][1], payment, a.account.interest_rate_apr,
                              n - DETAILED_MONTHS)
        for k, pay in enumerate(payoff):
            for account_id, share in payers:
                end_flow(account_id, DETAILED_MONTHS + k, -pay * share / payment)

    # Goal overlays that end (a target date) and lumps; a budget's or an
    # open-ended goal's overlay is part of the steady net.
    def overlay_flow(o: Overlay, m: int, amount: Decimal):
        if o.from_pool:
            ending[None][m] -= amount
        if o.goal_account_id is not None and o.goal_account_id in ending:
            ending[o.goal_account_id][m] += amount if o.from_pool else -amount

    goals_by_id = {g.id: g for g in enabled}
    last_gap: dict[int, Overlay] = {}
    for o in proj.overlays:
        goal = goals_by_id.get(o.goal_id)
        if goal is None or (goal.kind != PlanGoalKind.LUMP.value and goal.target_date is None):
            continue
        m = at(o.on)
        if m is not None:
            overlay_flow(o, m, o.amount)
        if goal.kind != PlanGoalKind.LUMP.value and m == DETAILED_MONTHS - 1:
            last_gap[goal.id] = o
    # Past the walk, a monthly goal keeps month 23's top-up until its target date.
    for goal_id, o in last_gap.items():
        for m in range(DETAILED_MONTHS, n):
            if months[m] > goals_by_id[goal_id].target_date:
                break
            overlay_flow(o, m, o.amount)

    r = _monthly_rate(return_pct)
    i = _monthly_rate(inflation_pct)
    inflate = [(1 + i) ** m for m in range(n)]

    # Retirement draws, in today's money inflated to each month: on top of the
    # walk's nets as well as the steady net, and never in its average.
    draws: dict[int | None, list[Decimal]] = {b: [_ZERO] * n for b in buckets}
    for g in enabled:
        if (g.kind == PlanGoalKind.RETIREMENT.value and g.account_id in proj.goal_accounts
                and g.target_date and g.monthly_amount):
            start = at(g.target_date) if g.target_date >= months[0] else 0
            if start is not None:
                for m in range(start, n):
                    draws[g.account_id][m] -= g.monthly_amount * inflate[m]

    def flows(bucket) -> tuple[Decimal, list[Decimal]]:
        """(opening, monthly flows): the walk's nets, then the steady net plus ending flows."""
        ends = _month_ends(buckets[bucket])
        opening = sum((by_id[a].seed for a in (proj.pool if bucket is None else {bucket})), _ZERO)
        nets, previous = [], opening
        for m in range(DETAILED_MONTHS):
            end = ends.get(months[m], previous)  # no accounts: a single day-0 point
            nets.append(end - previous)
            previous = end
        steady = sum((nets[m] - ending[bucket][m] for m in range(12, DETAILED_MONTHS)),
                     _ZERO) / 12
        return opening, [
            (nets[m] if m < DETAILED_MONTHS else steady + ending[bucket][m]) + draws[bucket][m]
            for m in range(n)
        ]

    flow_of = {bucket: flows(bucket) for bucket in buckets}
    series = {bucket: _grow(opening, f, r) for bucket, (opening, f) in flow_of.items()}

    nominal = series[None]
    real = [v / inflate[m] for m, v in enumerate(nominal)]
    depleted = next((m for m, v in enumerate(nominal) if v < 0), None)
    ages = [None if birth_year is None else month.year - birth_year for month in months]

    required = []
    for g in enabled:
        if g.account_id not in proj.goal_accounts or g.target_date is None:
            continue
        trajectory = series[g.account_id]
        past = g.target_date < months[0]
        t = 0 if past else at(g.target_date)
        if t is None:
            continue  # beyond the domain
        if g.kind == PlanGoalKind.SAVINGS_TARGET.value and g.target_amount:
            checks = [(t, g.target_amount)]
        elif g.kind == PlanGoalKind.LUMP.value and g.target_amount:
            checks = [(t, _ZERO)]
        elif g.kind == PlanGoalKind.RETIREMENT.value and g.monthly_amount:
            checks = [(m, _ZERO) for m in range(t, n)]
        else:
            continue
        # Saving runs through the target month, as ``project``'s top-up does.
        required.append(GoalRequirement(g.id, required_contribution(
            trajectory, checks, contribute_until=0 if past else t + 1, r=r)))

    return Lifetime(
        months=months,
        ages=ages,
        nominal=[_q(v) for v in nominal],
        real=[_q(v) for v in real],
        goal_accounts={b: [_q(v) for v in series[b]] for b in proj.goal_accounts},
        depletion_month=months[depleted] if depleted is not None else None,
        depletion_age=ages[depleted] if depleted is not None else None,
        required=required,
    )


def required_contribution(
    trajectory: Sequence[Decimal],
    checks: Sequence[tuple[int, Decimal]],
    *,
    contribute_until: int,
    r: Decimal,
) -> Decimal | None:
    """The least extra monthly saving ``c``, paid in months ``0..contribute_until-1``,
    for which ``trajectory[m] + c * S(m) >= floor`` at every ``(m, floor)`` check.

    Exact, not searched: a balance compounding at ``r`` is linear in ``c``, with
    ``S(m) = sum((1 + r) ** (m - k) for k paid up to m)``. None when a check
    falls short and no month is left to pay in.
    """
    need = _ZERO
    for m, floor in checks:
        short = floor - trajectory[m]
        if short <= 0:
            continue
        slope = sum(((1 + r) ** (m - k) for k in range(min(m + 1, contribute_until))), _ZERO)
        if slope == 0:
            return None
        need = max(need, short / slope)
    return _q(need)


def _q(value: Decimal) -> Decimal:
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)
