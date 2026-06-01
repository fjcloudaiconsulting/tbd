"""Unit tests for the pure billing cycle-window helper.

current_cycle_window(cycle_day, today) -> (start, end_inclusive), derived
purely from the org's billing_cycle_day. No DB I/O. This is the canonical
period boundary shared by generation, get_current_period, and
ensure_future_periods.
"""
from __future__ import annotations

from datetime import date

from app.services.billing_service import current_cycle_window


def test_cycle_day_1_mid_month():
    start, end = current_cycle_window(1, date(2026, 6, 15))
    assert start == date(2026, 6, 1)
    assert end == date(2026, 6, 30)


def test_today_before_cycle_day_steps_back_a_month():
    start, end = current_cycle_window(15, date(2026, 6, 5))
    assert start == date(2026, 5, 15)
    assert end == date(2026, 6, 14)


def test_today_on_cycle_day_starts_today():
    start, end = current_cycle_window(15, date(2026, 6, 15))
    assert start == date(2026, 6, 15)
    assert end == date(2026, 7, 14)


def test_cycle_day_31_clamps_in_short_month():
    start, end = current_cycle_window(31, date(2026, 2, 10))
    assert start == date(2026, 1, 31)
    assert end == date(2026, 2, 27)


def test_cycle_day_31_when_today_is_month_end():
    start, end = current_cycle_window(31, date(2026, 1, 31))
    assert start == date(2026, 1, 31)
    assert end == date(2026, 2, 27)


def test_january_steps_back_into_previous_year():
    start, end = current_cycle_window(15, date(2026, 1, 5))
    assert start == date(2025, 12, 15)
    assert end == date(2026, 1, 14)
