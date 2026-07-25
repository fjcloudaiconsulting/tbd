"""Pure loan projected-payment math (Loan Account Type V1, Slice 2).

DB-free, mirroring ``cc_forecast_service``: all arithmetic for the loan
forecast synthesis lives here so it is unit-testable without a database.
``account_balance_forecast_service`` supplies the batch-fetched inputs
(the already-paid signal, the reused PMT) and applies the resulting deltas.

Design (``specs/2026-07-24-loan-account-type-v1-design.md`` §4, O2 resolved
2026-07-25 by two architect design validations = Design A, current-balance +
period-skip):

  outstanding = max(0, -loan.balance)     # owed stored NEGATIVE, CURRENT balance
  applied     = min(PMT, outstanding)     # never project a loan below zero
  source.expected -= applied ; loan.expected += applied   # conserving (Σ==0)

ONE payment per period. The forecast covers a single ~1-month billing period,
so a monthly loan has at most one scheduled date in-window; we project only the
EARLIEST and log if a second ever appears (rather than ship the ill-defined
skip-vs-thread composition the CC ``s_prev``/``p_k_owned`` machinery would need
-- that precision is CC-specific and Loan V1 has no partial/strategy system).

``already_paid`` is computed by the caller as the presence of a linked
payment-in leg on the loan in-period (``balance_contribution_filter``, NO status
filter -> catches settled AND pending; see the caller). It is load-bearing:
because ``outstanding`` uses the CURRENT balance (which already reflects a
settled payment) and the forecast already folds pending payments into
``pending_delta``, projecting again would double-count. Skipping the period is
the correct guard.

Intended limitations (documented so nobody "fixes" a non-bug):
  * Interest-blind: the projection moves the loan by the full ``applied`` and
    models no interest accrual (V1 has no accrual-posting engine; the loan
    balance only moves on a recorded payment). If an accrual job is ever added,
    the synthesis must also add the accrual leg or it over-projects paydown.
  * A recorded PARTIAL payment (< PMT) suppresses the whole period (skip),
    under-projecting rather than topping up -- out of the V1 model; under-
    projecting is safer than double-counting.
  * Bare-expense limitation: a payment booked as a plain expense on the source
    with no linked loan leg is undetectable -> possible phantom. Identical to
    the CC synthesis's dependence on linked legs; consistent, not a new defect.
  * A loan still owing past ``term_months`` projects nothing (the ``k <
    term_months`` bound), so an underpaid/interest_only loan does not emit
    phantom post-maturity payments.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import structlog
from dateutil.relativedelta import relativedelta

logger = structlog.get_logger(__name__)


def due_loan_payment_dates(
    first_payment_date: date,
    term_months: int,
    p_start: date,
    p_end: date,
) -> list[date]:
    """Scheduled payment dates ``d_k`` in ``[p_start, p_end]`` with 0 <= k < term.

    ``d_k = first_payment_date + relativedelta(months=k)`` is computed from the
    fixed anchor for each k (NOT incrementally) so month-length drift never
    accumulates (e.g. Jan 31 + 2 months = Mar 31, not Feb-clamped-then-drifted).
    ``k < term_months`` bounds at the final scheduled payment (k=term_months-1 =
    maturation date); k==term_months and beyond are excluded.
    """
    dates: list[date] = []
    for k in range(term_months):
        d_k = first_payment_date + relativedelta(months=k)
        if d_k > p_end:
            break  # d_k is monotonic in k; nothing further can be in-window
        if d_k >= p_start:
            dates.append(d_k)
    return dates


def synthesize_account_loan_payment(
    *,
    first_payment_date: date,
    term_months: int,
    balance: Decimal,
    pmt: Decimal,
    p_start: date,
    p_end: date,
    already_paid: bool,
    account_id: int | None = None,
) -> list[tuple[date, Decimal]]:
    """(payment_date, applied) for one loan this period -- a list of 0 or 1.

    Returns ``[]`` when the period's payment is already accounted for
    (``already_paid``), when there is no scheduled date in-window, or when the
    loan is paid off (``outstanding == 0``). Returns a single ``(date, applied)``
    otherwise, projecting only the earliest in-window date (logs if a second is
    found). List return shape mirrors ``cc_forecast_service`` so the caller
    wires both synthesizers identically.
    """
    if already_paid:
        return []
    dates = due_loan_payment_dates(first_payment_date, term_months, p_start, p_end)
    if not dates:
        return []
    if len(dates) > 1:
        # Not reachable for a monthly loan in a single-month period; log rather
        # than silently thread (the skip signal is period-scoped and cannot
        # attribute a credit to one of several in-window dates).
        logger.warning(
            "loan_forecast.multiple_due_dates_in_window",
            account_id=account_id,
            count=len(dates),
            p_start=p_start.isoformat(),
            p_end=p_end.isoformat(),
        )
    pay_date = dates[0]
    outstanding = max(Decimal("0"), -Decimal(str(balance)))
    applied = min(pmt, outstanding)
    if applied <= 0:
        return []
    return [(pay_date, applied)]
