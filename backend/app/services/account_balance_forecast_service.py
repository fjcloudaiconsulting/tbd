"""Per-account expected month-end balance projection.

Dashboard-only view. Distinct from forecast_service.compute_forecast,
which deals with reportable income/expense aggregates (and excludes
transfer legs / manual adjustments). This module answers a different
question:

  "What will each account's balance be at the end of this billing period?"

Account balance is the sum of all settled transactions on the account,
including transfer legs. Pending transactions on the account haven't
moved the stored balance yet but will. So the projection is simply:

  expected_account_balance = stored_balance + sum(pending deltas in period)

with sign by type (income +, expense -). Transfer legs MUST be included
because they DO move balances per-account, even though they're not
reportable income/expense. Manual adjustments are settled-only by design,
but we filter them out defensively from the pending delta in case that
ever changes.

Currency totals are grouped: never sum unlike currencies.

── Day resolution (TBD-198) ─────────────────────────────────────────────────

The month-end number above is the LAST POINT of a daily series, not a second
computation beside it. ``daily_balances`` walks ``[max(p_start, today) ..
window_end]`` accumulating every delta source the projection knows about, and
``expected_month_end_balance`` is literally that walk's final running total.
The invariant

    daily_balances[-1].balance == expected_month_end_balance

therefore holds by construction, for every account, always. Do NOT "optimise"
the total back into a standalone ``balance + pending + synth`` expression: the
series and the card's headline number would then disagree on the same row,
which is the failure mode this shape exists to make impossible.

``risk_days`` is a READ of that series (below-zero runs), never its own
projection.
"""

import datetime
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountType
from app.models.cc_cycle_payment import CcCyclePayment
from app.models.recurring import RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.services import cc_forecast_service, loan_forecast_service
from app.services.billing_service import period_spend_window_end, resolve_period
from app.services.cc_statement_service import load_cc_ledgers
from app.services.date_utils import occurrences_in_window
from app.services.loan_service import compute_pmt
from app.services.recurring_filters import active_series_filter, remaining_occurrences
from app.services.transaction_filters import (
    balance_contribution_filter,
    effective_period_date_expr,
)

# ── Low-balance warning: the deny-list (TBD-198) ─────────────────────────────
# Account types where a negative balance is NORMAL, not a warning. A deny-list,
# NOT an allow-list, and NOT a reuse of ``PAYMENT_SOURCE_ALLOWED_SLUGS``:
# user-created account types carry ``slug = NULL`` (``models/account.py``), so
# an allow-list would silently drop every custom type out of the warning and
# the feature would be invisible to exactly the users who named their own
# accounts.
LIABILITY_SLUGS: frozenset[str] = frozenset({"credit_card", "loan"})

# The threshold is ZERO and there is no setting for it in v1. Zero is
# currency-free (0 EUR == 0 USD == overdrawn), needs no storage, no migration
# and no form, and it is a fact rather than a judgement. A user buffer is
# deferred to v1.1 as an org-level ``OrgSetting`` (precedent:
# ``services/scheduler/org_settings.get_cc_statement_lead_days``). Per-account
# thresholds are rejected.
LOW_BALANCE_THRESHOLD = Decimal("0")


async def compute_account_balance_forecast(
    db: AsyncSession,
    org_id: int,
    *,
    period_start: datetime.date | None = None,
    today: datetime.date | None = None,
) -> dict:
    """Compute expected month-end balance per account for a billing period.

    ``today`` is keyword-only and injectable because the period window floors
    at the wall clock (see ``window_end`` below); it is resolved once, here.

    Returns the spec shape:

        {
          "period_start": "YYYY-MM-DD",
          "series_start": "YYYY-MM-DD",
          "period_end": "YYYY-MM-DD",
          "totals": [{currency, balance, pending_delta, expected_month_end_balance}],
          "accounts": [{account_id, account_name, currency, is_default,
                        account_type_slug, balance, pending_delta,
                        expected_month_end_balance, daily_balances, risk_days}],
        }

    ``series_start`` is ``daily_balances[0].date`` — the day the walk seeds on,
    which is NOT ``period_start`` once the clock has moved into the period. It
    is emitted because ``_add_day_delta`` floors every overdue delta onto that
    day: without it the client cannot tell "day 0 is 200 lower than yesterday"
    from "day 0 is where three slipped obligations were re-booked".

    ``daily_balances`` / ``risk_days`` live on the ACCOUNT rows only. Nothing
    risk-shaped goes on ``totals``: each account has exactly one currency, so a
    per-account series never sums anything, and ``totals`` is the one place a
    cross-currency sum could enter.
    """
    today = today if today is not None else datetime.date.today()

    # Threaded (TBD-297): `resolve_period`'s fallback arm reaches
    # `get_current_period`, which auto-creates. This is the sibling of the same
    # fix in `forecast_service` — same router, same reachability — and leaving
    # one of the two forecast surfaces unthreaded is how a ticket ends up
    # fencing half a problem.
    period = await resolve_period(db, org_id, period_start, today=today)

    p_start = period.start_date

    # ── The period's ONE window (TBD-243) ─────────────────────────────────
    # Same single value as `forecast_service.compute_forecast`, for the same
    # reason: it bounds the pending aggregate, the CC synthesis horizon, the
    # loan already-paid probe, the loan synthesis horizon AND the emitted
    # `period_end`. Splitting the settled-sum bound from the projection
    # horizon is rejected by
    # `specs/2026-07-30-forecast-period-window-design.md` §2 (it breaks
    # conservation across `generate_due_transactions`), and every
    # anti-double-count guard here — the loan `already_paid` probe below, CC's
    # `p_k_owned` — has to sit on the window anyway.
    #
    # Closed rows verbatim, never floored (§4 F6). The `None` arm is the
    # roster tail; the calendar expression there keeps `period_end` non-null
    # and keeps `col <= window_end` compilable (§4 F5).
    if period.end_date is not None:
        window_end = period.end_date
    else:
        derived = await period_spend_window_end(db, org_id, period, today=today)
        window_end = derived if derived is not None else (
            p_start + relativedelta(months=1) - datetime.timedelta(days=1)
        )

    # ── The daily series' first day (TBD-198) ─────────────────────────────
    # ⚠ ``window_end`` above is UNCHANGED and must stay that way; this is a
    # separate, additive quantity that never feeds back into it.
    #
    # The series starts at the later of the period start and the clock: days
    # already behind us are current state, not a projection. The ``min`` is
    # NOT cosmetic — a past period has ``today > window_end``, and without the
    # clamp the walk is empty, ``daily_balances[-1]`` does not exist and the
    # series/total invariant becomes unstatable exactly where the widget
    # renders its neutral non-current-period state. Clamped, a past period
    # yields a one-point series whose single point IS the month-end number.
    walk_start = min(max(p_start, today), window_end)

    accounts_result = await db.execute(
        select(Account, AccountType.slug)
        .join(AccountType, Account.account_type_id == AccountType.id)
        .where(
            Account.org_id == org_id,
            Account.is_active.is_(True),
        )
    )
    rows = accounts_result.all()

    # Aggregate pending transactions in the selected period by account.
    # Sign by type: income +, expense -. Include transfer legs (this is
    # per-account balance math, not reportable). Defensively exclude
    # manual adjustments (settled-only today).
    #
    # TBD-198: the aggregate additionally groups by ``eff_date`` so the SAME
    # rows can be laid on the day they land. ``pending_by_account`` (the
    # reported ``pending_delta``) is the day map summed back up, never a
    # second query — two queries is how the column and the series would come
    # to disagree.
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
            and_(eff_date >= p_start, eff_date <= window_end),
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
    # moves toward zero. Synthesized HERE (per-account balances include transfer
    # legs), never in forecast_service (reportable aggregate excludes them).
    # The synthesis is CONSERVING within a currency (the source drops by exactly
    # what the liability gains), so it cancels out of the per-currency
    # ``expected`` total; cross-currency pairs are skipped so the rollup never
    # desyncs. (Since TBD-198 that total is Σ(rows) rather than
    # ``balance + pending`` — see ``bucket["expected"]`` below — so the
    # cancellation is now a property of the numbers rather than of the
    # expression.)
    accounts_by_id = {acct.id: (acct, slug) for acct, slug in rows}
    cc_accounts = [
        acct for acct, slug in rows
        if slug == "credit_card"
        and acct.close_day is not None
        and acct.payment_source_account_id is not None
    ]
    cc_payments_by_account: dict[int, list[dict]] = {}

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
        # fetch at window_end would then drop ledger rows in
        # (window_end, close_date] that balance_at_close(close_date) needs,
        # silently under-counting outstanding. This matches the
        # pre-refactor inline query, which
        # was also unbounded and let balance_at_close's own close_date
        # re-filter do the work.
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
                cc, p_start=p_start, p_end=window_end,
                opening_balance=Decimal(str(cc.opening_balance)),
                ledger=ledger_by_account.get(cc.id, []),
                credits=credits_by_account.get(cc.id, []),
                per_cycle_amounts=per_cycle_amounts,
            )
            for pay_date, outflow in payments:
                _add_day_delta(day_deltas, source.id, pay_date, -outflow, walk_start)
                _add_day_delta(day_deltas, cc.id, pay_date, outflow, walk_start)
                cc_payments_by_account.setdefault(cc.id, []).append(
                    {"amount": _q(outflow), "date": pay_date.isoformat()})

    # ── Loan projected-payment synthesis (Slice 2) ────────────────────────────
    # Same conserving shape as the CC synthesis above (source drops, loan moves
    # toward zero on the scheduled payment date), booked into the SAME
    # ``day_deltas`` map so a source that funds both a CC and a loan keeps both
    # deltas. Design A (period-skip): outstanding uses the CURRENT
    # balance and we SKIP the period when a loan payment-in leg is already
    # accounted for. See loan_forecast_service for the O2 rationale.
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
    loan_payments_by_account: dict[int, list[dict]] = {}

    if loan_accounts:
        loan_ids = [a.id for a in loan_accounts]

        # Already-paid signal: any linked INCOME (payment-in) leg on the loan
        # whose effective date falls in the period. balance_contribution_filter()
        # carries NO status filter, so this catches BOTH settled legs (already
        # in loan.balance) and pending legs (already in pending_delta) -- DO NOT
        # add a status filter here or a pending loan payment double-counts
        # against pending_delta (regression-tested). Disbursement legs are an
        # EXPENSE on the loan and never match; manual adjustments are unlinked.
        loan_paid_rows = (await db.execute(
            select(Transaction.account_id).distinct()
            .where(Transaction.org_id == org_id,
                   Transaction.account_id.in_(loan_ids),
                   Transaction.linked_transaction_id.is_not(None),
                   Transaction.type == TransactionType.INCOME,
                   balance_contribution_filter(),
                   and_(eff_date >= p_start, eff_date <= window_end))
        )).all()
        loan_paid_ids = {aid for (aid,) in loan_paid_rows}

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
            payments = loan_forecast_service.synthesize_account_loan_payment(
                first_payment_date=loan.first_payment_date,
                term_months=int(loan.term_months),
                balance=Decimal(str(loan.balance)),
                pmt=pmt,
                p_start=p_start,
                p_end=window_end,
                already_paid=loan.id in loan_paid_ids,
                account_id=loan.id,
            )
            for pay_date, applied in payments:
                _add_day_delta(day_deltas, source.id, pay_date, -applied, walk_start)
                _add_day_delta(day_deltas, loan.id, pay_date, applied, walk_start)
                loan_payments_by_account.setdefault(loan.id, []).append(
                    {"amount": _q(applied), "date": pay_date.isoformat()})

    # ── Upcoming recurring, keyed by ACCOUNT (TBD-198) ───────────────────────
    # ⚠ Before TBD-198 this service did not see recurring templates AT ALL: it
    # met them only after ``generate_due_transactions`` had materialised them
    # into rows. A daily series built on that blind spot MOVES the moment the
    # scheduler ticks — the TBD-260 defect class, on a new surface. So the
    # projection is ported here, keyed by ``r.account_id`` where
    # ``forecast_service`` keys by ``r.category_id``.
    #
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
    # beyond ``<= window_end``: ``next_due_date`` is a FRONTIER, not an
    # occurrence date. See ``forecast_service`` for the full argument.
    recurring_result = await db.execute(
        select(RecurringTransaction).where(
            RecurringTransaction.org_id == org_id,
            active_series_filter(),
            RecurringTransaction.next_due_date <= window_end,
        )
    )
    recurring_items = list(recurring_result.scalars().all())

    materialised: set[tuple[int, datetime.date]] = set()
    if recurring_items:
        materialised_rows = await db.execute(
            select(Transaction.recurring_id, Transaction.date).where(
                Transaction.org_id == org_id,
                Transaction.recurring_id.in_([r.id for r in recurring_items]),
                Transaction.date >= p_start,
                Transaction.date <= window_end,
            )
        )
        materialised = {(rid, _as_date(d)) for rid, d in materialised_rows.all()}

    # No `recurring_by_account` TOTAL on purpose: a per-account sum kept beside
    # the day map is a second place the same number lives. What IS emitted is
    # the LINES themselves — one dict per projected occurrence, appended in the
    # very same statement pair that books the day delta, so the two cannot
    # drift apart without an edit that deletes one of two adjacent lines.
    #
    # They are emitted because ``expected_month_end_balance`` is Σ(all delta
    # sources) and the card renders a sub-line for the CC and loan halves of
    # that sum but had none for this half: the user saw a month-end number
    # 350 below the balance with nothing on screen naming the 350. That is the
    # exact thing PRODUCT.md's line-item-visibility principle forbids.
    #
    # ⚠ The line carries the OCCURRENCE date ``d``, not the day the delta is
    # booked on. ``_add_day_delta`` floors an overdue occurrence onto
    # ``walk_start``, so for a slipped occurrence the two differ. The
    # occurrence date is the honest answer to "what is this line?" (and matches
    # the CC/loan lines, which also carry their own resolved due date); the
    # floor is made visible instead by ``series_start`` on the response.
    recurring_lines_by_account: dict[int, list[dict]] = {}
    for r in recurring_items:
        if r.account_id is None or r.account_id not in accounts_by_id:
            continue  # template on an inactive/unknown account: no row to move
        for d in occurrences_in_window(
            r.next_due_date, r.frequency, p_start, window_end,
            budget=remaining_occurrences(r),
        ):
            if (r.id, d) in materialised:
                continue
            amount = Decimal(str(r.amount))
            delta = amount if r.type == "income" else -amount
            _add_day_delta(day_deltas, r.account_id, d, delta, walk_start)
            recurring_lines_by_account.setdefault(r.account_id, []).append(
                {"amount": _q(delta), "date": d.isoformat()}
            )

    # ── Day-0 seed correction: future-dated SETTLED rows (TBD-198) ───────────
    # ⚠ ``accounts.balance`` is DATE-AGNOSTIC. It is written at transaction-
    # write time (``transaction_service``), its invariant has no date term, and
    # nothing forbids a future-dated transaction. So a settled row dated the
    # 25th is ALREADY inside ``balance`` today. Seeding day 0 with ``balance``
    # verbatim therefore pre-spends future outflows and pre-banks future
    # income, and the series reads as if the whole month had already happened.
    #
    # The fix subtracts those rows from the seed and lays them back on their
    # own effective dates, so the walk's FINAL value is unchanged (both ends
    # must be asserted, or the fence proves nothing — the final value is
    # identical either way).
    #
    # Bounded at ``window_end`` on purpose: rows dated beyond the window are
    # left in the seed, which is the pre-existing behaviour, unchanged here.
    # ``balance_contribution_filter()`` is the set that actually makes up
    # ``accounts.balance`` — manual adjustments included, reconcile-matched
    # duplicates excluded.
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
            and_(eff_date > walk_start, eff_date <= window_end),
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

    accounts_payload: list[dict] = []
    totals_by_currency: dict[str, dict[str, Decimal]] = {}

    sorted_rows = sorted(
        rows,
        key=lambda r: (
            not r[0].is_default,
            r[0].name.casefold(),
            r[0].id,
        ),
    )

    for account, type_slug in sorted_rows:
        balance = Decimal(str(account.balance))
        delta = pending_by_account.get(account.id, Decimal("0"))

        # ONE projection (TBD-198). ``expected_month_end_balance`` is the walk's
        # final running total, not a parallel expression — see the module
        # docstring. Restoring ``balance + delta + synth`` here is precisely the
        # mutant F1 exists to kill: it silently drops the recurring projection
        # and the series and the headline disagree on the same row.
        seed = balance + seed_adjustment.get(account.id, Decimal("0"))
        daily_balances, running = _walk_daily(
            seed, day_deltas.get(account.id, {}), walk_start, window_end
        )
        expected = running

        accounts_payload.append(
            {
                "account_id": account.id,
                "account_name": account.name,
                "currency": account.currency,
                "is_default": account.is_default,
                "account_type_slug": type_slug,
                "balance": _q(balance),
                "pending_delta": _q(delta),
                "expected_month_end_balance": _q(expected),
                "cc_payments": cc_payments_by_account.get(account.id, []),
                "loan_payments": loan_payments_by_account.get(account.id, []),
                # Sorted by date: the templates are iterated in query order, so
                # a monthly and a weekly series on the same account would
                # otherwise interleave arbitrarily on the card.
                "recurring_lines": sorted(
                    recurring_lines_by_account.get(account.id, []),
                    key=lambda line: line["date"],
                ),
                "daily_balances": [
                    {"date": d.isoformat(), "balance": _q(b)} for d, b in daily_balances
                ],
                "risk_days": _risk_runs(daily_balances, type_slug, today),
            }
        )

        bucket = totals_by_currency.setdefault(
            account.currency,
            {
                "balance": Decimal("0"),
                "pending_delta": Decimal("0"),
                "expected": Decimal("0"),
            },
        )
        bucket["balance"] += balance
        bucket["pending_delta"] += delta
        # Σ(rows) by construction, so the hero number and the rows under it
        # cannot drift. Same-currency only — ``account.currency`` keys the
        # bucket, so nothing here ever sums unlike currencies. (Before TBD-198
        # this was `balance + pending_delta`, which held only because CC/loan
        # synthesis CONSERVES within a currency; a recurring occurrence does
        # not conserve — it has no counterpart leg — so the sum has to be
        # carried rather than re-derived.)
        bucket["expected"] += expected

    totals_payload = [
        {
            "currency": currency,
            "balance": _q(b["balance"]),
            "pending_delta": _q(b["pending_delta"]),
            "expected_month_end_balance": _q(b["expected"]),
        }
        for currency, b in sorted(totals_by_currency.items())
    ]

    return {
        "period_start": p_start.isoformat(),
        "series_start": walk_start.isoformat(),
        "period_end": window_end.isoformat(),
        "totals": totals_payload,
        "accounts": accounts_payload,
    }


_TWOPLACES = Decimal("0.01")


def _q(value: Decimal) -> str:
    """Format a Decimal as a fixed 2-decimal string for JSON transport."""
    return str(value.quantize(_TWOPLACES))


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
    already bounded above by ``window_end``, but several can be dated BEFORE
    the clock and still be genuinely outstanding: a pending row whose estimated
    settle date has slipped, an un-materialised recurring occurrence whose
    frontier lags, a projected CC/loan payment whose due date has passed
    unpaid. Dropping those would make the series' final value disagree with the
    month-end total; booking them on day 0 keeps the total exact and is honest
    about what day 0 means (end of today, obligations included).

    ⚠ **What the floor does and does not guarantee.** It cannot manufacture a
    warning ON day 0 or before it: a run that contains day 0 starts at or
    before ``today`` and R3 drops it. It CAN change whether a LATER day is
    warned, and that is not a bug: a slipped 1,200 outflow dated yesterday is
    dropped entirely when unfloored, and lowers every day of the series from
    day 0 onward when floored — so a future day that would have stayed just
    above zero can now cross it. The obligation is real and outstanding, so the
    floored answer is the correct one; the earlier claim that the floor "cannot
    manufacture a false warning" overstated it by ignoring exactly this case.
    ``series_start`` on the response is what lets a client see that day 0
    carries re-booked overdue deltas rather than one day's activity.
    """
    if on < walk_start:
        on = walk_start
    per_day = day_deltas.setdefault(account_id, {})
    per_day[on] = per_day.get(on, Decimal("0")) + delta


def _walk_daily(
    seed: Decimal,
    per_day: dict[datetime.date, Decimal],
    walk_start: datetime.date,
    window_end: datetime.date,
) -> tuple[list[tuple[datetime.date, Decimal]], Decimal]:
    """End-of-day balances for ``[walk_start .. window_end]`` inclusive.

    ⚠ **End of day, not per event (R1).** One point per day, emitted only after
    EVERY delta dated that day has been applied. Rent on the 1st and salary on
    the 1st net out to a day that never dipped; an event-ordered walk would
    report an intraday trough that no bank statement will ever show, and that
    single mistake would make the warning fire on the most common shape in the
    dataset.
    """
    series: list[tuple[datetime.date, Decimal]] = []
    running = seed
    day = walk_start
    while day <= window_end:
        running += per_day.get(day, Decimal("0"))
        series.append((day, running))
        day += datetime.timedelta(days=1)
    return series, running


def _risk_runs(
    series: list[tuple[datetime.date, Decimal]],
    type_slug: str | None,
    today: datetime.date,
) -> list[dict]:
    """Below-threshold RUNS read off an already-computed daily series.

    A read, never a second projection. Four rules, all of them the product:

    * **R1 — end of day only.** Inherited: ``series`` is already one point per
      day, post-netting.
    * **R2 — runs, not days.** Each contiguous below-threshold interval is ONE
      entry. One entry per day would turn a two-week overdraft into fourteen
      warnings and the card into noise.
    * **R3 — strictly future.** ``run_from > today``. A dip today or in the
      past is CURRENT STATE and is already shown, exactly, in the Balance
      column; repeating it as a "warning" tells the user nothing they cannot
      see and costs the warning its credibility.
    * **R4 — already-negative accounts are re-warned, not warned.** An account
      below zero today is inside a run that starts at or before today, so R3
      drops that run and only a SUBSEQUENT crossing survives. R3 and R4 are one
      comparison here, deliberately — but they are two different claims, and
      F7's dip/recover/dip fixture is what proves the second one, because an
      implementation that reports only the global minimum passes a fixture with
      a single run.

      ⚠ **R4 is defined for the CURRENT period only, and only the current
      period renders it.** With ``today < p_start`` (a FUTURE period, reachable
      at the API via ``?period_start=<future>``) the walk seeds on *today's*
      balance and nothing between today and ``p_start`` is modelled, so an
      account at −50 today IS reported as a future run — the shape R4 exists to
      suppress. This is API-contract-only: ``AccountMonthEndForecast`` renders
      a neutral "current period only" state for any non-current period, so no
      user ever sees it. It is recorded rather than suppressed because
      suppressing ``risk_days`` for ``today < p_start`` would delete the ONLY
      clock under which the ``LIABILITY_SLUGS`` deny-list is discriminable
      (F6): with ``today == p_start`` every liability run starts on day 0 and
      R3 suppresses it anyway, which is how F6's first draft came out green
      against its own mutant.

    The comparison is STRICT (``< 0``) and is made on the QUANTIZED balance,
    the same 2dp value ``daily_balances`` puts on the wire. ``0.00`` is not
    overdrawn; ``-0.01`` is. ``<=`` would flag every account that lands exactly
    on zero, and comparing the raw running total would flag a
    ``-0.0001`` — a run whose every rendered figure reads ``-0.00``.
    """
    if type_slug in LIABILITY_SLUGS:
        # A negative balance is the NORMAL state of a card or a loan; warning
        # on it would fire every single day and train the user to ignore the
        # badge everywhere else.
        return []

    runs: list[dict] = []
    current: dict | None = None
    for day, raw in series:
        # Quantize FIRST: `daily_balances` is `_q`'d on the wire, so a raw
        # -0.0001 would open a run every one of whose rendered figures is
        # "-0.00". Unreachable today (every delta source is a 2dp money
        # column) and one percentage-based payment strategy away from being
        # reachable.
        balance = raw.quantize(_TWOPLACES)
        if balance < LOW_BALANCE_THRESHOLD:
            if current is None:
                current = {
                    "from": day,
                    "through": day,
                    "lowest_balance": balance,
                    "lowest_on": day,
                }
            else:
                current["through"] = day
                if balance < current["lowest_balance"]:
                    current["lowest_balance"] = balance
                    current["lowest_on"] = day
        elif current is not None:
            runs.append(current)
            current = None
    if current is not None:
        runs.append(current)

    return [
        {
            "from": r["from"].isoformat(),
            "through": r["through"].isoformat(),
            "lowest_balance": _q(r["lowest_balance"]),
            "lowest_on": r["lowest_on"].isoformat(),
        }
        for r in runs
        if r["from"] > today
    ]
