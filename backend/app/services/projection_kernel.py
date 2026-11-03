"""The one multi-month balance projection (TBD-599).

Shared by the dashboard's per-account month-end forecast
(``account_balance_forecast_service``) and, from later TBD-548 tickets, Plans.
Lifted verbatim from ``account_balance_forecast_service`` so the two surfaces
cannot disagree about what the books already say will happen:

  * pending rows, laid on their effective date;
  * un-materialised recurring occurrences (``occurrences_in_window``);
  * credit-card cycle and loan payment synthesis;
  * future-dated SETTLED rows, taken out of the day-0 seed and laid back on
    their own dates.

``baseline`` walks ``[walk_start .. horizon_end]`` one day at a time and emits
a point per day for months 0-23 (month 0 is ``walk_start``'s calendar month)
and a point per month-end after that. The last point is always
``horizon_end``, so ``points[-1]`` IS the projection's final running total.

Per account and per currency, never across currencies: every account has one
currency, and ``Baseline.currencies`` sums an account only into its own
currency's series.
"""

import datetime
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountType
from app.models.cc_cycle_payment import CcCyclePayment
from app.models.recurring import RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.services import cc_forecast_service, loan_forecast_service
from app.services.cc_statement_service import load_cc_ledgers
from app.services.date_utils import occurrences_in_window
from app.services.loan_service import compute_pmt
from app.services.recurring_filters import active_series_filter, remaining_occurrences
from app.services.transaction_filters import (
    balance_contribution_filter,
    effective_period_date_expr,
    non_reverted_transaction_filter,
)

# Months 0..DAILY_MONTHS-1 get one point per day; later months one point each.
DAILY_MONTHS = 24

Point = tuple[datetime.date, Decimal]


@dataclass
class AccountBaseline:
    account: Account
    type_slug: str | None
    balance: Decimal
    pending_delta: Decimal
    # (date, amount) lines behind the projection; amounts are signed for
    # recurring, and positive outflows for the CC and loan payments.
    cc_payments: list[Point]
    loan_payments: list[Point]
    recurring_lines: list[Point]
    points: list[Point]

    @property
    def end_balance(self) -> Decimal:
        return self.points[-1][1]


@dataclass
class Baseline:
    walk_start: datetime.date
    horizon_end: datetime.date
    accounts: list[AccountBaseline]
    # currency -> points, summed over that currency's accounts only.
    currencies: dict[str, list[Point]]


async def baseline(
    db: AsyncSession,
    org_id: int,
    *,
    today: datetime.date,
    horizon_end: datetime.date,
    exclude_recurring_ids: Iterable[int] = (),
    window_start: datetime.date | None = None,
    first_period_end: datetime.date | None = None,
) -> Baseline:
    """Project every active account of ``org_id`` up to ``horizon_end``.

    ``window_start`` (default ``today``) is the lower bound of every delta
    source; deltas dated before the clock are floored onto day 0 (see
    ``_add_day_delta``). The dashboard passes its billing-period start.

    ``exclude_recurring_ids`` drops those series entirely: their frontier
    (no projected occurrences) AND their already-generated PENDING rows.
    Settled rows of the series are history and stay in the balance.

    ``first_period_end`` closes the first loan period (default: one month
    after ``window_start``); later periods are consecutive months after it.
    A loan pays at most once per period and skips a period that already has
    a payment-in leg, exactly as the single-period dashboard always did.

    Beyond-horizon settled rows stay in the seed (the dashboard's long-standing
    behaviour), so a SHORTER horizon can read a month-end slightly differently
    from a longer one when a settled row is dated past the shorter horizon.
    """
    window_start = window_start if window_start is not None else today
    excluded = sorted(set(exclude_recurring_ids))

    # The series starts at the later of the window start and the clock: days
    # already behind us are current state, not a projection. The ``min`` is
    # NOT cosmetic: a past window has ``today > horizon_end``, and without the
    # clamp the walk is empty and ``points[-1]`` does not exist. Clamped, a
    # past window yields a one-point series whose single point IS the total.
    walk_start = min(max(window_start, today), horizon_end)

    accounts_result = await db.execute(
        select(Account, AccountType.slug)
        .join(AccountType, Account.account_type_id == AccountType.id)
        .where(
            Account.org_id == org_id,
            Account.is_active.is_(True),
        )
    )
    rows = accounts_result.all()

    # Aggregate pending transactions in the window by account. Sign by type:
    # income +, expense -. Include transfer legs (this is per-account balance
    # math, not reportable). Defensively exclude manual adjustments
    # (settled-only today).
    #
    # TBD-198: the aggregate groups by ``eff_date`` so the SAME rows can be
    # laid on the day they land. ``pending_by_account`` (the reported
    # ``pending_delta``) is the day map summed back up, never a second query.
    eff_date = effective_period_date_expr()
    pending_result = await db.execute(
        select(
            Transaction.account_id,
            Transaction.type,
            eff_date.label("eff"),
            func.coalesce(func.sum(Transaction.amount), Decimal("0")),
        )
        .where(
            Transaction.org_id == org_id,
            Transaction.status == TransactionStatus.PENDING,
            Transaction.is_manual_adjustment.is_(False),
            # TBD-272: a skipped (or rejected) PENDING row will never land.
            non_reverted_transaction_filter(),
            and_(eff_date >= window_start, eff_date <= horizon_end),
            # `NOT IN` alone is NULL for a non-recurring row and would drop it.
            *([or_(
                Transaction.recurring_id.is_(None),
                Transaction.recurring_id.not_in(excluded),
            )] if excluded else []),
        )
        .group_by(Transaction.account_id, Transaction.type, eff_date)
    )

    pending_by_account: dict[int, Decimal] = {}
    day_deltas: dict[int, dict[datetime.date, Decimal]] = {}
    for account_id, tx_type, eff, total in pending_result.all():
        delta = Decimal(str(total or 0))
        if tx_type == TransactionType.EXPENSE:
            delta = -delta
        pending_by_account[account_id] = (
            pending_by_account.get(account_id, Decimal("0")) + delta
        )
        _add_day_delta(day_deltas, account_id, _as_date(eff), delta, walk_start)

    # ── Credit-card projected-payment synthesis (Slice 3) ─────────────────────
    # Ephemeral in-memory deltas with provenance source="credit_card_payment":
    # on each resolved due date the source asset drops and the CC liability
    # moves toward zero. The synthesis is CONSERVING within a currency (the
    # source drops by exactly what the liability gains); cross-currency pairs
    # are skipped so the per-currency rollup never desyncs. One call threads
    # ``s_prev`` across every cycle of the horizon, so a multi-month window
    # needs nothing more than a later ``p_end``.
    accounts_by_id = {acct.id: (acct, slug) for acct, slug in rows}
    cc_accounts = [
        acct for acct, slug in rows
        if slug == "credit_card"
        and acct.close_day is not None
        and acct.payment_source_account_id is not None
    ]
    cc_payments_by_account: dict[int, list[Point]] = {}

    if cc_accounts:
        cc_ids = [a.id for a in cc_accounts]

        pcp_rows = (await db.execute(
            select(CcCyclePayment.account_id, CcCyclePayment.period_anchor_year,
                   CcCyclePayment.period_anchor_month, CcCyclePayment.amount)
            .where(CcCyclePayment.account_id.in_(cc_ids))
        )).all()
        per_cycle_amounts = {(aid, y, m): Decimal(str(amt)) for aid, y, m, amt in pcp_rows}

        # Single source of the CC ledger query (cc_statement_service):
        # UNBOUNDED (no up_to). A due cycle's payment_date is not
        # guaranteed to be >= its own close_date -- with payment_day <
        # close_day and payment_day_relative_month == 0 (same-month
        # payment), payment_date can fall BEFORE close_date. Bounding the
        # fetch at horizon_end would then drop ledger rows in
        # (horizon_end, close_date] that balance_at_close(close_date) needs,
        # silently under-counting outstanding.
        ledger_by_account = await load_cc_ledgers(db, org_id, cc_ids)

        credit_rows = (await db.execute(
            select(Transaction.id, Transaction.account_id, eff_date.label("eff"), Transaction.amount)
            .where(Transaction.org_id == org_id,
                   Transaction.account_id.in_(cc_ids),
                   Transaction.linked_transaction_id.is_not(None),
                   Transaction.type == TransactionType.INCOME,
                   balance_contribution_filter())
        )).all()
        credits_by_account: dict[int, list[tuple]] = {}
        for cid, aid, eff, amt in credit_rows:
            credits_by_account.setdefault(aid, []).append((cid, eff, Decimal(str(amt))))

        for cc in cc_accounts:
            source_entry = accounts_by_id.get(cc.payment_source_account_id)
            if source_entry is None:
                continue  # source inactive/not loaded -> no-op (do not resurrect)
            source, _ = source_entry
            if source.currency != cc.currency:
                continue  # no FX in V1 -> would desync per-currency totals
            payments = cc_forecast_service.synthesize_account_cc_payments(
                cc, p_start=window_start, p_end=horizon_end,
                opening_balance=Decimal(str(cc.opening_balance)),
                ledger=ledger_by_account.get(cc.id, []),
                credits=credits_by_account.get(cc.id, []),
                per_cycle_amounts=per_cycle_amounts,
            )
            for pay_date, outflow in payments:
                _add_day_delta(day_deltas, source.id, pay_date, -outflow, walk_start)
                _add_day_delta(day_deltas, cc.id, pay_date, outflow, walk_start)
                cc_payments_by_account.setdefault(cc.id, []).append((pay_date, outflow))

    # ── Loan projected-payment synthesis (Slice 2) ────────────────────────────
    # Same conserving shape as the CC synthesis above (source drops, loan moves
    # toward zero on the scheduled payment date), booked into the SAME
    # ``day_deltas`` map so a source that funds both a CC and a loan keeps both
    # deltas. Design A (period-skip): outstanding uses the CURRENT balance and
    # we SKIP a period when a loan payment-in leg is already accounted for.
    # See loan_forecast_service for the O2 rationale.
    loan_accounts = [
        acct for acct, slug in rows
        if slug == "loan"
        and acct.payment_source_account_id is not None
        and acct.principal_amount is not None
        and acct.interest_rate_apr is not None
        and acct.term_months is not None
        and acct.origination_date is not None
        and acct.first_payment_date is not None
    ]
    loan_payments_by_account: dict[int, list[Point]] = {}

    if loan_accounts:
        loan_ids = [a.id for a in loan_accounts]
        periods = _loan_periods(window_start, first_period_end, horizon_end)

        # Already-paid signal: any linked INCOME (payment-in) leg on the loan
        # whose effective date falls in the period. balance_contribution_filter()
        # carries NO status filter, so this catches BOTH settled legs (already
        # in loan.balance) and pending legs (already in pending_delta) -- DO NOT
        # add a status filter here or a pending loan payment double-counts
        # against pending_delta (regression-tested). Disbursement legs are an
        # EXPENSE on the loan and never match; manual adjustments are unlinked.
        loan_paid_rows = (await db.execute(
            select(Transaction.account_id, eff_date.label("eff")).distinct()
            .where(Transaction.org_id == org_id,
                   Transaction.account_id.in_(loan_ids),
                   Transaction.linked_transaction_id.is_not(None),
                   Transaction.type == TransactionType.INCOME,
                   balance_contribution_filter(),
                   and_(eff_date >= window_start, eff_date <= horizon_end))
        )).all()
        loan_paid = {(aid, _as_date(eff)) for aid, eff in loan_paid_rows}

        for loan in loan_accounts:
            source_entry = accounts_by_id.get(loan.payment_source_account_id)
            if source_entry is None:
                continue  # source inactive/not loaded -> no-op (do not resurrect)
            source, _ = source_entry
            if source.currency != loan.currency:
                continue  # no FX in V1 -> would desync per-currency totals
            pmt = compute_pmt(
                Decimal(str(loan.principal_amount)),
                Decimal(str(loan.interest_rate_apr)),
                int(loan.term_months),
            )
            # shortcut: later periods start from the current balance plus the
            # payments synthesised so far, blind to pending payment legs in
            # earlier periods; thread the pending legs when Plans needs it.
            balance = Decimal(str(loan.balance))
            for seg_start, seg_end in periods:
                payments = loan_forecast_service.synthesize_account_loan_payment(
                    first_payment_date=loan.first_payment_date,
                    term_months=int(loan.term_months),
                    balance=balance,
                    pmt=pmt,
                    p_start=seg_start,
                    p_end=seg_end,
                    already_paid=any(
                        aid == loan.id and seg_start <= d <= seg_end
                        for aid, d in loan_paid
                    ),
                    account_id=loan.id,
                )
                for pay_date, applied in payments:
                    balance += applied
                    _add_day_delta(day_deltas, source.id, pay_date, -applied, walk_start)
                    _add_day_delta(day_deltas, loan.id, pay_date, applied, walk_start)
                    loan_payments_by_account.setdefault(loan.id, []).append(
                        (pay_date, applied))

    # ── Upcoming recurring, keyed by ACCOUNT (TBD-198) ───────────────────────
    # All THREE guards are load-bearing and each removes a named, already-fixed
    # defect; dropping any one reintroduces it:
    #   * ``active_series_filter()``  — TBD-275: an exhausted instalment series
    #     keeps ``is_active = True`` (exhaustion is derived), so ``is_active``
    #     alone projects a finished 12-month plan forever.
    #   * ``budget=remaining_occurrences(r)`` — TBD-275: the fast-forward spends
    #     instalments just reaching the window, exactly as generation's
    #     unbounded catch-up loop does.
    #   * the ``materialised`` set — TBD-260: generation's own create-condition,
    #     negated. Without it every occurrence is counted twice the instant it
    #     is generated.
    #
    # There is deliberately NO clock predicate and NO ``next_due_date`` bound
    # beyond ``<= horizon_end``: ``next_due_date`` is a FRONTIER, not an
    # occurrence date. See ``forecast_service`` for the full argument.
    recurring_result = await db.execute(
        select(RecurringTransaction).where(
            RecurringTransaction.org_id == org_id,
            active_series_filter(),
            RecurringTransaction.next_due_date <= horizon_end,
            *([RecurringTransaction.id.not_in(excluded)] if excluded else []),
        )
    )
    recurring_items = list(recurring_result.scalars().all())

    materialised: set[tuple[int, datetime.date]] = set()
    if recurring_items:
        materialised_rows = await db.execute(
            select(Transaction.recurring_id, Transaction.date).where(
                Transaction.org_id == org_id,
                Transaction.recurring_id.in_([r.id for r in recurring_items]),
                Transaction.date >= window_start,
                Transaction.date <= horizon_end,
            )
        )
        materialised = {(rid, _as_date(d)) for rid, d in materialised_rows.all()}

    # The LINES are appended in the very same statement pair that books the day
    # delta, so the two cannot drift apart. A line carries the OCCURRENCE date
    # ``d``, not the day the delta is booked on (``_add_day_delta`` floors an
    # overdue occurrence onto ``walk_start``).
    recurring_lines_by_account: dict[int, list[Point]] = {}
    for r in recurring_items:
        if r.account_id is None or r.account_id not in accounts_by_id:
            continue  # template on an inactive/unknown account: no row to move
        for d in occurrences_in_window(
            r.next_due_date, r.frequency, window_start, horizon_end,
            budget=remaining_occurrences(r),
        ):
            if (r.id, d) in materialised:
                continue
            amount = Decimal(str(r.amount))
            delta = amount if r.type == "income" else -amount
            _add_day_delta(day_deltas, r.account_id, d, delta, walk_start)
            recurring_lines_by_account.setdefault(r.account_id, []).append((d, delta))

    # ── Day-0 seed correction: future-dated SETTLED rows (TBD-198) ───────────
    # ⚠ ``accounts.balance`` is DATE-AGNOSTIC: a settled row dated the 25th is
    # ALREADY inside ``balance`` today. Seeding day 0 with ``balance`` verbatim
    # would pre-spend future outflows and pre-bank future income. Those rows
    # are subtracted from the seed and laid back on their own effective dates,
    # so the walk's FINAL value is unchanged.
    #
    # Bounded at ``horizon_end`` on purpose: rows dated beyond it are left in
    # the seed. ``balance_contribution_filter()`` is the set that actually
    # makes up ``accounts.balance`` — manual adjustments included,
    # reconcile-matched duplicates excluded.
    future_settled_rows = (await db.execute(
        select(
            Transaction.account_id,
            Transaction.type,
            eff_date.label("eff"),
            func.coalesce(func.sum(Transaction.amount), Decimal("0")),
        )
        .where(
            Transaction.org_id == org_id,
            Transaction.status == TransactionStatus.SETTLED,
            balance_contribution_filter(),
            and_(eff_date > walk_start, eff_date <= horizon_end),
        )
        .group_by(Transaction.account_id, Transaction.type, eff_date)
    )).all()

    seed_adjustment: dict[int, Decimal] = {}
    for account_id, tx_type, eff, total in future_settled_rows:
        delta = Decimal(str(total or 0))
        if tx_type == TransactionType.EXPENSE:
            delta = -delta
        seed_adjustment[account_id] = (
            seed_adjustment.get(account_id, Decimal("0")) - delta
        )
        _add_day_delta(day_deltas, account_id, _as_date(eff), delta, walk_start)

    daily_end = min(
        horizon_end,
        walk_start.replace(day=1) + relativedelta(months=DAILY_MONTHS)
        - datetime.timedelta(days=1),
    )
    accounts: list[AccountBaseline] = []
    currencies: dict[str, list[Point]] = {}
    for account, type_slug in rows:
        balance = Decimal(str(account.balance))
        seed = balance + seed_adjustment.get(account.id, Decimal("0"))
        points = _walk(seed, day_deltas.get(account.id, {}), walk_start, daily_end, horizon_end)
        accounts.append(AccountBaseline(
            account=account,
            type_slug=type_slug,
            balance=balance,
            pending_delta=pending_by_account.get(account.id, Decimal("0")),
            cc_payments=cc_payments_by_account.get(account.id, []),
            loan_payments=loan_payments_by_account.get(account.id, []),
            recurring_lines=recurring_lines_by_account.get(account.id, []),
            points=points,
        ))
        # Every account walks the same dates, so a currency series is the
        # index-wise sum of ITS accounts' points and nothing else.
        total = currencies.get(account.currency)
        currencies[account.currency] = list(points) if total is None else [
            (d, b + p) for (d, b), (_, p) in zip(total, points)
        ]

    return Baseline(
        walk_start=walk_start,
        horizon_end=horizon_end,
        accounts=accounts,
        currencies=currencies,
    )


def _loan_periods(
    window_start: datetime.date,
    first_period_end: datetime.date | None,
    horizon_end: datetime.date,
) -> list[tuple[datetime.date, datetime.date]]:
    """``[window_start, first_period_end]``, then whole months after it, clipped
    at ``horizon_end``. Months are computed from one anchor, never chained, so
    a 31st does not drift through February."""
    first_end = first_period_end if first_period_end is not None else (
        window_start + relativedelta(months=1) - datetime.timedelta(days=1)
    )
    periods = [(window_start, min(first_end, horizon_end))]
    anchor = first_end + datetime.timedelta(days=1)
    k = 0
    while anchor + relativedelta(months=k) <= horizon_end:
        start = anchor + relativedelta(months=k)
        end = anchor + relativedelta(months=k + 1) - datetime.timedelta(days=1)
        periods.append((start, min(end, horizon_end)))
        k += 1
    return periods


def _as_date(value) -> datetime.date:
    """Coerce a DB-returned effective date to ``datetime.date``.

    ``effective_period_date_expr()`` is a ``COALESCE`` and some drivers hand
    back a string rather than a date for it. Every caller here uses the value
    as a dict key, so one un-coerced string would split a day into two buckets
    that never merge.
    """
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return datetime.date.fromisoformat(str(value)[:10])


def _add_day_delta(
    day_deltas: dict[int, dict[datetime.date, Decimal]],
    account_id: int,
    on: datetime.date,
    delta: Decimal,
    walk_start: datetime.date,
) -> None:
    """Book ``delta`` against ``account_id`` on ``on``, floored at ``walk_start``.

    The floor is what makes the walk conserve. Every delta source here is
    already bounded above by ``horizon_end``, but several can be dated BEFORE
    the clock and still be genuinely outstanding: a pending row whose estimated
    settle date has slipped, an un-materialised recurring occurrence whose
    frontier lags, a projected CC/loan payment whose due date has passed
    unpaid. Dropping those would make the series' final value disagree with
    the total; booking them on day 0 keeps the total exact and is honest about
    what day 0 means (end of today, obligations included).

    ⚠ The floor CAN change whether a LATER day dips below zero: a slipped
    1,200 outflow dated yesterday lowers every day from day 0 onward. The
    obligation is real and outstanding, so the floored answer is the correct
    one. ``series_start`` on the dashboard response is what lets a client see
    that day 0 carries re-booked overdue deltas rather than one day's activity.
    """
    if on < walk_start:
        on = walk_start
    per_day = day_deltas.setdefault(account_id, {})
    per_day[on] = per_day.get(on, Decimal("0")) + delta


def _walk(
    seed: Decimal,
    per_day: dict[datetime.date, Decimal],
    walk_start: datetime.date,
    daily_end: datetime.date,
    horizon_end: datetime.date,
) -> list[Point]:
    """End-of-day balances: every day to ``daily_end``, then month-ends, then
    ``horizon_end``.

    ⚠ **End of day, not per event (R1).** A point is emitted only after EVERY
    delta dated that day has been applied. Rent on the 1st and salary on the
    1st net out to a day that never dipped; an event-ordered walk would report
    an intraday trough that no bank statement will ever show.
    """
    series: list[Point] = []
    running = seed
    day = walk_start
    while day <= horizon_end:
        running += per_day.get(day, Decimal("0"))
        nxt = day + datetime.timedelta(days=1)
        if day <= daily_end or nxt.day == 1 or day == horizon_end:
            series.append((day, running))
        day = nxt
    return series
