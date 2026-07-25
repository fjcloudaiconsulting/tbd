"""Loan Account Type V1 Slice 2 — loan_forecast_service pure unit tests.

DB-free: date enumeration + the synthesize decision (skip / cap / earliest).
"""
from __future__ import annotations

import datetime
from decimal import Decimal

from app.services.loan_forecast_service import (
    due_loan_payment_dates,
    synthesize_account_loan_payment,
)

P_START = datetime.date(2026, 5, 1)
P_END = datetime.date(2026, 5, 31)


# ── due_loan_payment_dates ────────────────────────────────────────────────


def test_single_date_in_window():
    # first payment this period (k=0).
    assert due_loan_payment_dates(datetime.date(2026, 5, 15), 60, P_START, P_END) == [
        datetime.date(2026, 5, 15)
    ]


def test_midlife_loan_date_in_window():
    # started a year ago; k=12 lands in the window.
    got = due_loan_payment_dates(datetime.date(2025, 5, 15), 60, P_START, P_END)
    assert got == [datetime.date(2026, 5, 15)]


def test_future_loan_no_date():
    # first payment after the window -> nothing.
    assert due_loan_payment_dates(datetime.date(2026, 7, 1), 60, P_START, P_END) == []


def test_term_end_bound_excludes_past_maturity():
    # 12-month loan first paid 2025-05-15 -> last payment k=11 = 2026-04-15,
    # which is BEFORE the window; k=12 (2026-05-15) is past term -> not emitted.
    assert due_loan_payment_dates(datetime.date(2025, 5, 15), 12, P_START, P_END) == []


def test_final_payment_is_projected_at_k_term_minus_1():
    # last scheduled payment (k=term-1) lands in-window -> projected.
    # first 2025-06-15, term 12 -> k=11 = 2026-05-15.
    assert due_loan_payment_dates(datetime.date(2025, 6, 15), 12, P_START, P_END) == [
        datetime.date(2026, 5, 15)
    ]


def test_month_end_clamp_from_anchor():
    # anchored from Jan 31: k=4 -> May 31 (not drifted). In [5/1, 5/31].
    assert due_loan_payment_dates(datetime.date(2026, 1, 31), 60, P_START, P_END) == [
        datetime.date(2026, 5, 31)
    ]


# ── synthesize_account_loan_payment ───────────────────────────────────────

_COMMON = dict(
    first_payment_date=datetime.date(2026, 5, 15),
    term_months=60,
    pmt=Decimal("232.00"),
    p_start=P_START,
    p_end=P_END,
)


def test_synth_projects_capped_pmt():
    out = synthesize_account_loan_payment(
        balance=Decimal("-10000.00"), already_paid=False, **_COMMON
    )
    assert out == [(datetime.date(2026, 5, 15), Decimal("232.00"))]


def test_synth_already_paid_skips():
    assert (
        synthesize_account_loan_payment(
            balance=Decimal("-10000.00"), already_paid=True, **_COMMON
        )
        == []
    )


def test_synth_paid_off_noop():
    assert (
        synthesize_account_loan_payment(
            balance=Decimal("0.00"), already_paid=False, **_COMMON
        )
        == []
    )
    # overpaid (positive) also no-op
    assert (
        synthesize_account_loan_payment(
            balance=Decimal("50.00"), already_paid=False, **_COMMON
        )
        == []
    )


def test_synth_final_installment_caps_at_outstanding():
    # owed 100 < PMT 232 -> applied = 100 (never drive the loan positive).
    out = synthesize_account_loan_payment(
        balance=Decimal("-100.00"), already_paid=False, **_COMMON
    )
    assert out == [(datetime.date(2026, 5, 15), Decimal("100.00"))]


def test_synth_no_date_in_window_noop():
    out = synthesize_account_loan_payment(
        balance=Decimal("-10000.00"),
        already_paid=False,
        first_payment_date=datetime.date(2026, 7, 1),
        term_months=60,
        pmt=Decimal("232.00"),
        p_start=P_START,
        p_end=P_END,
    )
    assert out == []


def test_synth_multiple_dates_projects_earliest_only():
    # A wide (2-month) window holds two monthly dates; only the earliest is
    # projected (the >1 case logs a warning; we assert single projection).
    out = synthesize_account_loan_payment(
        balance=Decimal("-10000.00"),
        already_paid=False,
        first_payment_date=datetime.date(2026, 5, 15),
        term_months=60,
        pmt=Decimal("232.00"),
        p_start=datetime.date(2026, 5, 1),
        p_end=datetime.date(2026, 6, 30),
    )
    assert out == [(datetime.date(2026, 5, 15), Decimal("232.00"))]
