"""Unit tests for cc_cycle_service.py — per-CC cycle resolver.

Covers the 5 spec edge cases from the Slice 1 test plan
(spec 2026-05-28-cc-billing-cycle.md § Slice 1 / D2 / D3):

(a) default close 25, target inside the cycle → correct period + payment.
(b) close day 31 in a 30-day month (e.g. April) → clamp to 30.
(c) close day 31 in February non-leap → clamp to 28; leap → 29.
(d) same-month payment: payment_day=25, payment_day_relative_month=0.
(e) leap-year Feb 29 close → handles correctly; non-leap year clamps to 28.

Plus a regression test that a non-CC account (close_day=None) raises
ValueError.

All tests are pure Python — no DB, no async. The service receives a
plain triple (close_day, payment_day, payment_day_relative_month) and a
target date; the dataclass result is fully deterministic.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.services.cc_cycle_service import CreditCardCycle, resolve_cycle


# ─── (a) Standard cycle: close 25, target inside the cycle ───────────────────


def test_default_close_25_target_inside_cycle():
    """Close day 25, target = Feb 10 (inside the Jan 26 – Feb 25 cycle).

    Expected:
      period_start           = Jan 26
      period_end_inclusive   = Feb 25
      payment_date           = Mar 1  (default: day 1, next month)
      source                 = "default"
    """
    cycle = resolve_cycle(
        close_day=25,
        payment_day=None,
        payment_day_relative_month=None,
        target_date=date(2026, 2, 10),
    )
    assert isinstance(cycle, CreditCardCycle)
    assert cycle.period_start == date(2026, 1, 26)
    assert cycle.period_end_inclusive == date(2026, 2, 25)
    assert cycle.payment_date == date(2026, 3, 1)
    assert cycle.source == "default"


def test_target_on_close_day_belongs_to_closing_cycle():
    """D2 — inclusive rule: a tx on the close day (Feb 25) belongs to
    the Feb cycle, NOT the next one."""
    cycle = resolve_cycle(
        close_day=25,
        payment_day=None,
        payment_day_relative_month=None,
        target_date=date(2026, 2, 25),
    )
    assert cycle.period_end_inclusive == date(2026, 2, 25)
    assert cycle.period_start == date(2026, 1, 26)


def test_target_day_after_close_opens_next_cycle():
    """D2 — the day after close starts a new cycle."""
    cycle = resolve_cycle(
        close_day=25,
        payment_day=None,
        payment_day_relative_month=None,
        target_date=date(2026, 2, 26),
    )
    assert cycle.period_start == date(2026, 2, 26)
    assert cycle.period_end_inclusive == date(2026, 3, 25)
    assert cycle.payment_date == date(2026, 4, 1)


# ─── (b) Close day 31 in a 30-day month (April) ──────────────────────────────


def test_close_day_31_in_april_clamps_to_30():
    """Close day 31, target = Apr 15.

    April has 30 days → period_end_inclusive clamps to Apr 30.
    Payment: default (day 1, next month) → May 1.
    Period start: previous close was Mar 31, so start = Apr 1.
    """
    cycle = resolve_cycle(
        close_day=31,
        payment_day=None,
        payment_day_relative_month=None,
        target_date=date(2026, 4, 15),
    )
    assert cycle.period_end_inclusive == date(2026, 4, 30)
    assert cycle.period_start == date(2026, 4, 1)
    assert cycle.payment_date == date(2026, 5, 1)


def test_close_day_31_in_june_clamps_to_30():
    """June also has 30 days."""
    cycle = resolve_cycle(
        close_day=31,
        payment_day=None,
        payment_day_relative_month=None,
        target_date=date(2026, 6, 10),
    )
    assert cycle.period_end_inclusive == date(2026, 6, 30)
    assert cycle.period_start == date(2026, 6, 1)


# ─── (c) Close day 31 in February — non-leap and leap ───────────────────────


def test_close_day_31_in_february_non_leap():
    """2026 is not a leap year → Feb close clamps to Feb 28.

    Target = Feb 10, close day 31.
    Prev close was Jan 31, so period_start = Feb 1.
    period_end_inclusive = Feb 28 (clamped from 31).
    payment_date = Mar 1 (default).
    """
    cycle = resolve_cycle(
        close_day=31,
        payment_day=None,
        payment_day_relative_month=None,
        target_date=date(2026, 2, 10),
    )
    assert cycle.period_end_inclusive == date(2026, 2, 28)
    assert cycle.period_start == date(2026, 2, 1)
    assert cycle.payment_date == date(2026, 3, 1)


def test_close_day_31_in_february_leap_year():
    """2028 is a leap year → Feb close clamps to Feb 29."""
    cycle = resolve_cycle(
        close_day=31,
        payment_day=None,
        payment_day_relative_month=None,
        target_date=date(2028, 2, 10),
    )
    assert cycle.period_end_inclusive == date(2028, 2, 29)
    assert cycle.period_start == date(2028, 2, 1)


# ─── (d) Same-month payment ───────────────────────────────────────────────────


def test_same_month_payment_relative_month_0():
    """payment_day=25, payment_day_relative_month=0 → payment on the 25th
    of the close month (same month as period_end_inclusive).

    Close day = 1 (cycle Jan 2 – Feb 1); payment month = Feb (relative 0 = same
    month as close), day 25 → Feb 25.
    """
    cycle = resolve_cycle(
        close_day=1,
        payment_day=25,
        payment_day_relative_month=0,
        target_date=date(2026, 1, 15),
    )
    assert cycle.period_end_inclusive == date(2026, 2, 1)
    assert cycle.payment_date == date(2026, 2, 25)
    assert cycle.source == "default"


def test_same_month_payment_close_25():
    """Close day 25, payment_day=25, relative_month=0 → payment on Feb 25
    (same month as Feb 25 close)."""
    cycle = resolve_cycle(
        close_day=25,
        payment_day=25,
        payment_day_relative_month=0,
        target_date=date(2026, 2, 10),
    )
    assert cycle.period_end_inclusive == date(2026, 2, 25)
    assert cycle.payment_date == date(2026, 2, 25)


# ─── (e) Leap year Feb 29 close ───────────────────────────────────────────────


def test_close_day_29_in_leap_year_feb():
    """Explicitly close on Feb 29 in a leap year (2028).

    Target = Feb 15, 2028.
    period_start = Feb 1 (day after Jan 29 close, which is Jan 29).
    period_end_inclusive = Feb 29.
    """
    # Feb 29 is valid in 2028 (leap). close_day=29 in Feb → Feb 29.
    cycle = resolve_cycle(
        close_day=29,
        payment_day=None,
        payment_day_relative_month=None,
        target_date=date(2028, 2, 15),
    )
    assert cycle.period_end_inclusive == date(2028, 2, 29)
    assert cycle.period_start == date(2028, 1, 30)
    assert cycle.payment_date == date(2028, 3, 1)


def test_close_day_29_in_non_leap_year_feb():
    """close_day=29 in a non-leap Feb (2026) clamps to Feb 28."""
    cycle = resolve_cycle(
        close_day=29,
        payment_day=None,
        payment_day_relative_month=None,
        target_date=date(2026, 2, 10),
    )
    assert cycle.period_end_inclusive == date(2026, 2, 28)


# ─── Payment date clamping ────────────────────────────────────────────────────


def test_payment_day_31_in_february_clamps():
    """payment_day=31, relative_month=1 (next month after close).

    Close day 25, target Feb 10 → close month Feb, payment month Mar.
    Mar has 31 days → payment_date = Mar 31.
    """
    cycle = resolve_cycle(
        close_day=25,
        payment_day=31,
        payment_day_relative_month=1,
        target_date=date(2026, 2, 10),
    )
    assert cycle.payment_date == date(2026, 3, 31)


def test_payment_day_31_in_april_clamps_to_30():
    """payment_day=31, relative_month=1. Close April 25 → payment May.
    May has 31 days → May 31 (no clamp needed here).
    For a case where clamp fires: close March 25, payment April.
    April has 30 days → Apr 30.
    """
    cycle = resolve_cycle(
        close_day=25,
        payment_day=31,
        payment_day_relative_month=1,
        target_date=date(2026, 3, 10),
    )
    # Close month = Mar, payment month = Apr (30 days), clamp 31→30.
    assert cycle.payment_date == date(2026, 4, 30)


# ─── Regression: non-CC account raises ValueError ────────────────────────────


def test_non_cc_account_raises_value_error():
    """close_day=None signals a non-CC account. Resolver must raise
    ValueError immediately."""
    with pytest.raises(ValueError, match="credit.card"):
        resolve_cycle(
            close_day=None,
            payment_day=None,
            payment_day_relative_month=None,
            target_date=date(2026, 2, 10),
        )


# ─── Default sentinel handling ───────────────────────────────────────────────


def test_explicit_defaults_match_null_defaults():
    """payment_day=1, payment_day_relative_month=1 (explicit) must produce
    the same result as both being None (resolver defaults)."""
    kw = dict(close_day=25, target_date=date(2026, 2, 10))
    cycle_null = resolve_cycle(**kw, payment_day=None, payment_day_relative_month=None)
    cycle_explicit = resolve_cycle(**kw, payment_day=1, payment_day_relative_month=1)
    assert cycle_null.payment_date == cycle_explicit.payment_date
    assert cycle_null.period_start == cycle_explicit.period_start
    assert cycle_null.period_end_inclusive == cycle_explicit.period_end_inclusive


# ─── Year-boundary cycle (Dec close → Jan payment) ───────────────────────────


def test_year_boundary_dec_close_jan_payment():
    """Close day 28, target Dec 15.

    Cycle: Nov 29 – Dec 28. Payment: Jan 1.
    """
    cycle = resolve_cycle(
        close_day=28,
        payment_day=None,
        payment_day_relative_month=None,
        target_date=date(2026, 12, 15),
    )
    assert cycle.period_start == date(2026, 11, 29)
    assert cycle.period_end_inclusive == date(2026, 12, 28)
    assert cycle.payment_date == date(2027, 1, 1)
