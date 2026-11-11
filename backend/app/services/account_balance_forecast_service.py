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

Since TBD-599 the delta sources and the walk live in
``projection_kernel.baseline``, shared with Plans; this module resolves the
period's window, calls the kernel over exactly that window, and shapes the
response.
"""

import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import projection_kernel
from app.services.billing_service import resolve_period
from app.services.currency_service import normalise_currency

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
    window_end = await projection_kernel.period_window_end(
        db, org_id, period, today=today
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
    # The clamp itself lives in ``projection_kernel.baseline`` and comes back
    # as ``walk_start``.

    # ONE projection (TBD-198, TBD-599). Every delta source and the daily walk
    # live in ``projection_kernel``, shared with Plans; this service only
    # shapes the period's window into it and the result into the payload.
    projection = await projection_kernel.baseline(
        db, org_id,
        today=today,
        horizon_end=window_end,
        window_start=p_start,
        first_period_end=window_end,
    )

    accounts_payload: list[dict] = []
    totals_by_currency: dict[str, dict[str, Decimal]] = {}

    sorted_rows = sorted(
        projection.accounts,
        key=lambda r: (
            not r.account.is_default,
            r.account.name.casefold(),
            r.account.id,
        ),
    )

    for row in sorted_rows:
        account = row.account
        # Canonical code: a legacy 'eur' row joins the EUR total (TBD-801).
        currency = normalise_currency(account.currency)
        balance = row.balance
        delta = row.pending_delta

        # ``expected_month_end_balance`` is the walk's final running total, not
        # a parallel expression — see the module docstring. Restoring
        # ``balance + delta + synth`` here is precisely the mutant F1 exists to
        # kill: it silently drops the recurring projection and the series and
        # the headline disagree on the same row.
        daily_balances = row.points
        expected = row.end_balance

        accounts_payload.append(
            {
                "account_id": account.id,
                "account_name": account.name,
                "currency": currency,
                "is_default": account.is_default,
                "account_type_slug": row.type_slug,
                "balance": _q(balance),
                "pending_delta": _q(delta),
                "expected_month_end_balance": _q(expected),
                "cc_payments": _lines(row.cc_payments),
                "loan_payments": _lines(row.loan_payments),
                # Sorted by date: the templates are iterated in query order, so
                # a monthly and a weekly series on the same account would
                # otherwise interleave arbitrarily on the card. The line
                # carries the OCCURRENCE date, not the floored booking day;
                # the floor is made visible by ``series_start`` instead.
                "recurring_lines": sorted(
                    _lines(row.recurring_lines), key=lambda line: line["date"],
                ),
                "daily_balances": [
                    {"date": d.isoformat(), "balance": _q(b)} for d, b in daily_balances
                ],
                "risk_days": _risk_runs(daily_balances, row.type_slug, today),
            }
        )

        bucket = totals_by_currency.setdefault(
            currency,
            {
                "balance": Decimal("0"),
                "pending_delta": Decimal("0"),
                "expected": Decimal("0"),
            },
        )
        bucket["balance"] += balance
        bucket["pending_delta"] += delta
        # Σ(rows) by construction, so the hero number and the rows under it
        # cannot drift. Same-currency only — the canonical ``currency`` keys the
        # bucket, so nothing here ever sums unlike currencies. (Before TBD-198
        # this was `balance + pending_delta`, which held only because CC/loan
        # synthesis CONSERVES within a currency; an income or expense
        # recurring occurrence does not conserve — it has no counterpart leg —
        # so the sum has to be carried rather than re-derived. A recurring
        # TRANSFER (TBD-600) does conserve within a currency: its two legs
        # cancel in this bucket.)
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
        "series_start": projection.walk_start.isoformat(),
        "period_end": window_end.isoformat(),
        "totals": totals_payload,
        "accounts": accounts_payload,
    }


_TWOPLACES = Decimal("0.01")


def _q(value: Decimal) -> str:
    """Format a Decimal as a fixed 2-decimal string for JSON transport."""
    return str(value.quantize(_TWOPLACES))


def _lines(lines: list[tuple[datetime.date, Decimal]]) -> list[dict]:
    return [{"amount": _q(amount), "date": d.isoformat()} for d, amount in lines]


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
