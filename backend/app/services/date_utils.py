"""Shared date utilities for recurring-frequency date advancement."""

import datetime

from dateutil.relativedelta import relativedelta

from app.models.recurring import Frequency

# Defensive cap on any walk over a recurring template's occurrence grid.
# ``recurring_service.MAX_CATCHUP_ITERATIONS`` is an ALIAS of this value, not a
# second constant: ``generate_due_transactions`` and ``occurrences_in_window``
# walk the same grid from the same origin, and a forecast that projected
# occurrences generation truncates away (or vice versa) would move
# ``forecast_net`` with no user action. One number, two call sites.
MAX_OCCURRENCE_ITERATIONS = 500


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


def occurrences_in_window(
    next_due: datetime.date,
    freq: Frequency,
    start: datetime.date,
    end: datetime.date,
    *,
    max_iterations: int = MAX_OCCURRENCE_ITERATIONS,
) -> list[datetime.date]:
    """Occurrence dates in ``[start, end]``, walked with ``advance_date`` from
    ``next_due``.

    Iterated, never closed-form. ``advance_date`` is PATH-DEPENDENT for
    month-end templates (Jan 31 -> Feb 28 -> Mar 28, not Mar 31), and
    ``generate_due_transactions`` walks the same way from the same origin
    (``recurring_service.py`` catch-up loop). A closed-form jump would disagree
    with the dates generation actually creates, and conservation is a claim
    about exactly those dates.

    ``next_due`` is a FRONTIER — the next un-materialised occurrence — not an
    occurrence date in its own right. It may sit arbitrarily far before
    ``start``; the fast-forward loop below is what turns it into the grid.
    Clamping it (``max(next_due, start)``) would shift the grid off the
    template's day-of-month and project dates generation never creates.

    The iteration budget is SINGLE and spans both loops from the same origin,
    so a pathologically stale template truncates here exactly where the
    generation catch-up loop truncates.
    """
    out: list[datetime.date] = []
    d = next_due
    iterations = 0

    while d < start:
        if iterations >= max_iterations:
            return out
        iterations += 1
        nxt = advance_date(d, freq)
        if nxt <= d:
            return out
        d = nxt

    while d <= end:
        if iterations >= max_iterations:
            break
        iterations += 1
        out.append(d)
        nxt = advance_date(d, freq)
        if nxt <= d:
            break
        d = nxt

    return out
