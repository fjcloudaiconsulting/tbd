"""Loan Account Type V1 (Slice 1) — loan_service unit tests.

Pure (no DB): validation rules + computed-metric math, including the edge
cases the spec calls out (r=0, interest_only, paid_off, mid-life balance,
day-of-month clamping, boundary ranges).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.services.loan_service import (
    compute_loan_metrics,
    compute_pmt,
    validate_loan_fields,
)


_VALID = dict(
    target_slug="loan",
    principal_amount=Decimal("10000.00"),
    interest_rate_apr=Decimal("6.00"),
    term_months=60,
    origination_date=date(2026, 1, 1),
    first_payment_date=date(2026, 1, 15),
)


# ── validation ────────────────────────────────────────────────────────────


def test_valid_loan_fields_pass():
    assert validate_loan_fields(**_VALID) is None


@pytest.mark.parametrize(
    "field",
    [
        "principal_amount",
        "interest_rate_apr",
        "term_months",
        "origination_date",
        "first_payment_date",
    ],
)
def test_non_loan_target_rejects_each_loan_field(field):
    kwargs = dict(
        target_slug="checking",
        principal_amount=None,
        interest_rate_apr=None,
        term_months=None,
        origination_date=None,
        first_payment_date=None,
    )
    kwargs[field] = _VALID[field]
    with pytest.raises(HTTPException) as exc:
        validate_loan_fields(**kwargs)
    assert exc.value.status_code == 422
    assert "only allowed on loan accounts" in exc.value.detail


def test_non_loan_target_all_null_is_valid():
    assert (
        validate_loan_fields(
            target_slug="checking",
            principal_amount=None,
            interest_rate_apr=None,
            term_months=None,
            origination_date=None,
            first_payment_date=None,
        )
        is None
    )


@pytest.mark.parametrize(
    "field",
    [
        "principal_amount",
        "interest_rate_apr",
        "term_months",
        "origination_date",
        "first_payment_date",
    ],
)
def test_loan_target_requires_each_field(field):
    kwargs = dict(_VALID)
    kwargs[field] = None
    with pytest.raises(HTTPException) as exc:
        validate_loan_fields(**kwargs)
    assert exc.value.status_code == 422
    assert "required for loan accounts" in exc.value.detail


@pytest.mark.parametrize("principal", [Decimal("0"), Decimal("-1.00")])
def test_principal_must_be_positive(principal):
    with pytest.raises(HTTPException) as exc:
        validate_loan_fields(**{**_VALID, "principal_amount": principal})
    assert exc.value.status_code == 422


@pytest.mark.parametrize("apr", [Decimal("-0.01"), Decimal("1000.00")])
def test_apr_out_of_range(apr):
    with pytest.raises(HTTPException):
        validate_loan_fields(**{**_VALID, "interest_rate_apr": apr})


@pytest.mark.parametrize("apr", [Decimal("0"), Decimal("999.99")])
def test_apr_boundaries_ok(apr):
    assert validate_loan_fields(**{**_VALID, "interest_rate_apr": apr}) is None


@pytest.mark.parametrize("term", [0, -1, 481])
def test_term_out_of_range(term):
    with pytest.raises(HTTPException):
        validate_loan_fields(**{**_VALID, "term_months": term})


@pytest.mark.parametrize("term", [1, 480])
def test_term_boundaries_ok(term):
    assert validate_loan_fields(**{**_VALID, "term_months": term}) is None


def test_first_payment_before_origination_rejected():
    with pytest.raises(HTTPException) as exc:
        validate_loan_fields(
            **{
                **_VALID,
                "origination_date": date(2026, 2, 1),
                "first_payment_date": date(2026, 1, 1),
            }
        )
    assert exc.value.status_code == 422


def test_first_payment_equal_origination_ok():
    assert (
        validate_loan_fields(
            **{
                **_VALID,
                "origination_date": date(2026, 1, 1),
                "first_payment_date": date(2026, 1, 1),
            }
        )
        is None
    )


# ── PMT ───────────────────────────────────────────────────────────────────


def test_pmt_standard_amortization():
    # 10,000 at 6% APR over 60 months = 193.33/month (well-known figure).
    assert compute_pmt(Decimal("10000"), Decimal("6"), 60) == Decimal("193.33")


def test_pmt_zero_interest_is_principal_over_term():
    assert compute_pmt(Decimal("1200"), Decimal("0"), 12) == Decimal("100.00")


# ── metrics ───────────────────────────────────────────────────────────────


def _metrics(**overrides):
    base = dict(
        principal_amount=Decimal("10000.00"),
        interest_rate_apr=Decimal("6.00"),
        term_months=60,
        origination_date=date(2026, 1, 1),
        first_payment_date=date(2026, 1, 15),
        balance=Decimal("-10000.00"),
        today=date(2026, 1, 1),
    )
    base.update(overrides)
    return compute_loan_metrics(**base)


def test_maturation_date_is_last_of_n_payments():
    m = _metrics()
    # first_payment 2026-01-15 + (60 - 1) months
    assert m.maturation_date == date(2030, 12, 15)


def test_maturation_clamps_end_of_month():
    m = _metrics(first_payment_date=date(2026, 1, 31), term_months=2)
    assert m.maturation_date == date(2026, 2, 28)


def test_total_interest_full_term():
    m = _metrics()
    # 193.33 * 60 - 10000
    assert m.total_interest == Decimal("1599.80")


def test_total_interest_clamped_non_negative_at_zero_rate():
    # 0% loan whose PMT doesn't divide evenly -> cent residual must not render
    # a negative total interest.
    m = _metrics(
        principal_amount=Decimal("1000.00"),
        interest_rate_apr=Decimal("0"),
        term_months=3,
        balance=Decimal("-1000.00"),
    )
    assert m.total_interest == Decimal("0.00")


def test_paid_off_when_balance_zero():
    m = _metrics(balance=Decimal("0.00"))
    assert m.status == "paid_off"
    assert m.projected_payoff_months == 0
    assert m.projected_payoff_date == date(2026, 1, 1)


def test_paid_off_when_overpaid_positive_balance():
    m = _metrics(balance=Decimal("50.00"))
    assert m.status == "paid_off"


def test_on_track_midlife_balance_unambiguous_months():
    # owed 5000 at 6% with the full-loan PMT 193.33 -> n_rem = 28 (27.75 ceil).
    m = _metrics(balance=Decimal("-5000.00"))
    assert m.status == "on_track"
    assert m.projected_payoff_months == 28
    assert m.projected_payoff_date is not None


def test_zero_interest_payoff_months():
    m = _metrics(
        principal_amount=Decimal("1200.00"),
        interest_rate_apr=Decimal("0"),
        term_months=12,
        balance=Decimal("-600.00"),
    )
    assert m.status == "on_track"
    assert m.expected_monthly_payment == Decimal("100.00")
    assert m.projected_payoff_months == 6


def test_interest_only_when_payment_below_interest():
    # Contractual PMT (~10.29) far below the interest on a huge live balance.
    m = _metrics(
        principal_amount=Decimal("1000.00"),
        interest_rate_apr=Decimal("12.00"),
        term_months=360,
        balance=Decimal("-100000.00"),
    )
    assert m.status == "interest_only"
    assert m.projected_payoff_date is None
    assert m.projected_payoff_months is None


def test_payoff_anchored_to_next_scheduled_payment_date():
    # first payment 2026-01-15, today 2026-01-20 -> next payment is 2026-02-15.
    m = _metrics(balance=Decimal("-5000.00"), today=date(2026, 1, 20))
    # n_rem = 28 -> payoff = 2026-02-15 + 27 months = 2028-05-15
    assert m.projected_payoff_date == date(2028, 5, 15)
