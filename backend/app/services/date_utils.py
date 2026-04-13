"""Shared date utilities for recurring-frequency date advancement."""

import datetime

from dateutil.relativedelta import relativedelta

from app.models.recurring import Frequency


def advance_date(current: datetime.date, freq: Frequency) -> datetime.date:
    """Advance a date by the given recurring frequency."""
    if freq == Frequency.WEEKLY:
        return current + datetime.timedelta(weeks=1)
    elif freq == Frequency.BIWEEKLY:
        return current + datetime.timedelta(weeks=2)
    elif freq == Frequency.MONTHLY:
        return current + relativedelta(months=1)
    elif freq == Frequency.QUARTERLY:
        return current + relativedelta(months=3)
    elif freq == Frequency.YEARLY:
        return current + relativedelta(years=1)
    return current + relativedelta(months=1)
