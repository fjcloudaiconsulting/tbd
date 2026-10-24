"""Loan field validation + computed metrics (Loan Account Type V1, Slice 1).

See ``specs/2026-07-24-loan-account-type-v1-design.md`` §3.3 / §3.6.

``validate_loan_fields`` mirrors ``credit_card_service.validate_credit_card_fields``:
a plain sync helper that raises ``HTTPException(422)`` on violation and returns
``None`` on success. Called from the accounts router create path and the shared
``_apply_non_type_fields`` update path against the resulting row state.

``compute_loan_metrics`` is pure math (no DB): PMT, maturation date, projected
payoff (solved from the LIVE owed balance), and total interest. It is exposed as
a nested ``loan`` object on ``AccountResponse``. No amortization schedule is
materialized in V1 (so no largest-remainder apportionment machinery here).

Money is ``Decimal`` throughout; the payoff solve takes one acknowledged
Decimal->float hop through ``math.log`` (``ceil`` absorbs the float noise except
at an exact-integer month boundary, acceptable for a projection).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from dateutil.relativedelta import relativedelta
from fastapi import HTTPException

from app import tracing

logger = logging.getLogger(__name__)

_LOAN = "loan"

# Ranges (belt-and-suspenders with the Pydantic Field constraints, which 422 at
# parse time; these guard the service path and any non-schema caller).
_APR_LO = Decimal("0")
_APR_HI = Decimal("999.99")
_TERM_LO = 1
_TERM_HI = 480
# BE-16. Loan dates are bounded so maturation (first payment + up to 479
# months) and the payoff projection stay inside ``date``'s range.
LOAN_DATE_MIN = date(1900, 1, 1)
LOAN_DATE_MAX = date(2200, 12, 31)

_CENTS = Decimal("0.01")


def validate_loan_fields(
    *,
    target_slug: Optional[str],
    principal_amount: Optional[Decimal],
    interest_rate_apr: Optional[Decimal],
    term_months: Optional[int],
    origination_date: Optional[date],
    first_payment_date: Optional[date],
) -> None:
    """Validate the five loan-only field values against the target slug.

    Raises ``HTTPException(422)`` on any violation; returns ``None`` on success.
    """
    fields = {
        "principal_amount": principal_amount,
        "interest_rate_apr": interest_rate_apr,
        "term_months": term_months,
        "origination_date": origination_date,
        "first_payment_date": first_payment_date,
    }

    if target_slug != _LOAN:
        # Non-loan target: every loan-only column must be NULL (symmetric with
        # the CC validator's non-CC guard).
        for name, value in fields.items():
            if value is not None:
                raise HTTPException(
                    status_code=422,
                    detail=f"{name} is only allowed on loan accounts",
                )
        return

    # Loan target: all five required.
    for name, value in fields.items():
        if value is None:
            raise HTTPException(
                status_code=422,
                detail=f"{name} is required for loan accounts",
            )

    if principal_amount <= 0:
        raise HTTPException(
            status_code=422, detail="principal_amount must be greater than 0"
        )
    if not (_APR_LO <= interest_rate_apr <= _APR_HI):
        raise HTTPException(
            status_code=422,
            detail="interest_rate_apr must be between 0 and 999.99",
        )
    if not (_TERM_LO <= term_months <= _TERM_HI):
        raise HTTPException(
            status_code=422,
            detail="term_months must be between 1 and 480",
        )
    for name in ("origination_date", "first_payment_date"):
        if not (LOAN_DATE_MIN <= fields[name] <= LOAN_DATE_MAX):
            raise HTTPException(
                status_code=422,
                detail=f"{name} must be between {LOAN_DATE_MIN} and {LOAN_DATE_MAX}",
            )
    if first_payment_date < origination_date:
        raise HTTPException(
            status_code=422,
            detail="first_payment_date must be on or after origination_date",
        )
    # BE-16. PMT rounds to cents, so a tiny principal over a long term can
    # round to 0.00, a payment that never repays anything (and divided by
    # zero in the payoff projection).
    if compute_pmt(principal_amount, interest_rate_apr, term_months) <= 0:
        raise HTTPException(
            status_code=422,
            detail="loan terms give a monthly payment of 0.00; raise principal_amount or shorten term_months",
        )


@dataclass(frozen=True)
class LoanMetrics:
    """Computed loan metrics (no storage). ``projected_payoff_date`` is None when
    ``status`` is ``interest_only`` (payment can never amortize the live balance).
    """

    expected_monthly_payment: Decimal
    maturation_date: date
    total_interest: Decimal
    projected_payoff_date: Optional[date]
    projected_payoff_months: Optional[int]
    status: str  # "on_track" | "paid_off" | "interest_only"


def _monthly_rate(interest_rate_apr: Decimal) -> Decimal:
    """Monthly rate as a Decimal: APR percent / 100 / 12."""
    return interest_rate_apr / Decimal("100") / Decimal("12")


def compute_pmt(
    principal_amount: Decimal, interest_rate_apr: Decimal, term_months: int
) -> Decimal:
    """Contractual monthly payment (PMT). ``r == 0`` degenerates to P / n."""
    r = _monthly_rate(interest_rate_apr)
    if r == 0:
        pmt = principal_amount / Decimal(term_months)
    else:
        factor = (Decimal("1") + r) ** term_months
        pmt = principal_amount * r * factor / (factor - Decimal("1"))
    return pmt.quantize(_CENTS, rounding=ROUND_HALF_UP)


def compute_loan_metrics(
    *,
    principal_amount: Decimal,
    interest_rate_apr: Decimal,
    term_months: int,
    origination_date: date,
    first_payment_date: date,
    balance: Decimal,
    today: Optional[date] = None,
) -> LoanMetrics:
    """Compute all loan metrics. ``balance`` is the live account balance (owed is
    NEGATIVE); everything else is the contractual loan terms. ``today`` is
    injectable for deterministic tests.
    """
    if today is None:
        today = date.today()

    r = _monthly_rate(interest_rate_apr)
    pmt = compute_pmt(principal_amount, interest_rate_apr, term_months)

    maturation_date = first_payment_date + relativedelta(months=term_months - 1)
    # Contractual interest is non-negative; clamp at 0 so the cent-rounding
    # residual of a near-zero-rate loan never renders a negative "total
    # interest" (e.g. P=1000, apr=0, term=3 -> pmt 333.33 -> -0.01).
    total_interest = max(
        Decimal("0"),
        (pmt * Decimal(term_months) - principal_amount),
    ).quantize(_CENTS, rounding=ROUND_HALF_UP)

    owed = -balance  # positive when money is owed

    if owed <= 0:
        # Paid off / overpaid (incl. a mid-life import already at zero).
        return LoanMetrics(
            expected_monthly_payment=pmt,
            maturation_date=maturation_date,
            total_interest=total_interest,
            projected_payoff_date=today,
            projected_payoff_months=0,
            status="paid_off",
        )

    if pmt <= r * owed:
        # Payment never covers the interest accrual -> never amortizes. Checked
        # before the r == 0 branch so a legacy pmt of 0.00 (validation now
        # refuses it, BE-16) lands here instead of dividing by zero.
        return LoanMetrics(
            expected_monthly_payment=pmt,
            maturation_date=maturation_date,
            total_interest=total_interest,
            projected_payoff_date=None,
            projected_payoff_months=None,
            status="interest_only",
        )
    elif r == 0:
        n_rem = math.ceil(owed / pmt)
    else:
        # n_rem = ceil( -ln(1 - r*B/PMT) / ln(1 + r) ). One Decimal->float hop.
        ratio = 1.0 - float(r) * float(owed) / float(pmt)
        n_rem = math.ceil(-math.log(ratio) / math.log(1.0 + float(r)))

    n_rem = max(n_rem, 1)
    # Anchor to the next scheduled payment date (deterministic; does not drift
    # with the day the page is opened, and stays consistent with maturation).
    # BE-16: computed, not walked month by month from first_payment_date.
    next_payment_date = _first_payment_on_or_after(first_payment_date, today)
    try:
        projected_payoff_date = next_payment_date + relativedelta(months=n_rem - 1)
    except (OverflowError, ValueError):
        # Further out than ``date`` can say (a payment barely above the
        # interest on a large live balance). Still amortizing, so on_track,
        # with no date; the UI renders "On track" without one.
        projected_payoff_date = None

    return LoanMetrics(
        expected_monthly_payment=pmt,
        maturation_date=maturation_date,
        total_interest=total_interest,
        projected_payoff_date=projected_payoff_date,
        projected_payoff_months=n_rem,
        status="on_track",
    )


def _first_payment_on_or_after(first_payment_date: date, today: date) -> date:
    """The first ``first_payment_date + k months`` (k >= 0) on or after ``today``."""
    if first_payment_date >= today:
        return first_payment_date
    k = (today.year - first_payment_date.year) * 12 + (today.month - first_payment_date.month)
    # relativedelta clamps the day (Jan 31 + 1 month = Feb 28), so k months can
    # land just before today; then k + 1 is the answer, as the walk found.
    candidate = first_payment_date + relativedelta(months=k)
    if candidate < today:
        candidate = first_payment_date + relativedelta(months=k + 1)
    return candidate


def loan_metrics_or_none(**kwargs) -> Optional[LoanMetrics]:
    """``compute_loan_metrics``, or ``None`` when a stored row's values cannot
    be projected (BE-16).

    Rows written before the date and payment bounds can still hold them. One
    such account used to make every accounts list for its org a 500; now it
    renders without its loan block, as an incomplete loan does.
    """
    try:
        return compute_loan_metrics(**kwargs)
    except (ArithmeticError, ValueError, OverflowError) as exc:
        logger.warning("loan.metrics.unavailable", extra={"error": tracing.error_summary(exc)})
        return None
