from datetime import date

from app.models.recurring import Frequency
from app.services.date_utils import advance_date


def test_advance_date_handles_weekly_and_biweekly_frequencies() -> None:
    current = date(2026, 4, 24)

    assert advance_date(current, Frequency.WEEKLY) == date(2026, 5, 1)
    assert advance_date(current, Frequency.BIWEEKLY) == date(2026, 5, 8)


def test_advance_date_handles_month_end_rollover() -> None:
    assert advance_date(date(2024, 1, 31), Frequency.MONTHLY) == date(2024, 2, 29)
    assert advance_date(date(2024, 8, 31), Frequency.QUARTERLY) == date(2024, 11, 30)


def test_advance_date_handles_leap_day_for_yearly_frequency() -> None:
    assert advance_date(date(2024, 2, 29), Frequency.YEARLY) == date(2025, 2, 28)


def test_advance_date_falls_back_to_monthly_for_unknown_frequency() -> None:
    assert advance_date(date(2026, 1, 15), "custom") == date(2026, 2, 15)
