"""Shared date utilities for recurring-frequency date advancement."""

import datetime

from dateutil.relativedelta import relativedelta

from app.models.recurring import Frequency

# Defensive cap on how many occurrences one walk may COLLECT out of a single
# window. ``recurring_service.MAX_CATCHUP_ITERATIONS`` is an ALIAS of this
# value, not a second literal, so the two walks over a template's occurrence
# grid are sized by one number rather than by two constants that drift apart.
#
# ⚠ It is NOT a cap on how far a walk may travel to REACH the window. The
# fast-forward loop in ``occurrences_in_window`` is deliberately UNCAPPED; see
# that docstring for why capping it was a correctness bug and not a safety net.
#
# ⚠ Not to be confused with ``occurrences_in_window``'s ``budget`` argument
# (TBD-275), which DOES bound the fast-forward. That one is not an iteration
# cap: it is the instalment series' remaining occurrence count, generation
# spends it identically, and it therefore removes occurrences rather than
# hiding them. Same docstring spells out the difference.
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
    budget: int | None = None,
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

    **The fast-forward loop carries NO iteration budget, deliberately.** It
    used to share one budget with the collect loop below, on the claim that
    "the two walks cannot truncate differently". That claim is true of a single
    snapshot and FALSE of the invariant it was offered to support, because the
    two caps are not the same kind of cap:

    * generation's cap bounds WORK and MAKES PROGRESS — the catch-up loop
      mutates ``next_due_date`` forward on every step, so a run that hits the
      cap leaves the frontier 500 steps nearer the window and the next run
      resumes from there;
    * a cap on this fast-forward bounds VISIBILITY and makes NO progress — on
      exhaustion it returned an empty list, so in-window occurrences became
      INVISIBLE rather than merely expensive, and nothing ever recovered them.

    Measured on the shared budget: a weekly template whose frontier sat 521
    occurrences before ``p_start`` (``POST /api/v1/recurring`` has no past-date
    guard on ``next_due_date``, so a single-digit year typo reaches it)
    projected ``recurring_expense == 0``; one scheduler tick advanced the
    frontier inside the budget and the same window then reported 500.00.
    ``forecast_net`` moved with no user action — the exact defect TBD-260
    exists to remove. Conservation held to 495 steps and broke at 496.

    The loop is inherently bounded without a cap: ``advance_date`` moves
    strictly forward for every frequency, the ``nxt <= d`` no-progress guard
    below is the real defence against a runaway, and the loop terminates at
    ``start``. ``forecast_plan_service.populate_from_sources`` already ships
    exactly this uncapped shape.

    The COLLECT loop keeps ``max_iterations``, and that is a different
    exposure: it bounds occurrences per WINDOW, and the window comes from the
    billing-period roster (the open period's start is app-derived from
    ``current_cycle_window``; closed periods are admin-created through an
    overlap-validated endpoint), not from an unvalidated user-supplied date.
    Reaching it needs a single period window longer than 500 steps of the
    template's frequency — ~9.6 years of weekly. See §11 of
    ``specs/2026-07-30-forecast-overdue-recurring-design.md``.

    ``budget`` (TBD-275) is the instalment series' REMAINING occurrence count,
    ``None`` for an open-ended series. It is spent by **BOTH loops**, and the
    fast-forward is the half that matters.

    ⚠ **This is not the cap the paragraphs above forbid, and the distinction is
    the whole ticket.** They forbid an ITERATION cap on the fast-forward, on
    the grounds that it bounds VISIBILITY: the occurrences it hides are ones
    ``generate_due_transactions`` still creates, so hiding them makes
    ``forecast_net`` move the moment the scheduler ticks. A SERIES BUDGET does
    the opposite — it makes those occurrences NONEXISTENT, and generation
    agrees, because ``generate_due_transactions`` spends the same budget on the
    same occurrences from the same origin. Nothing is hidden, so nothing moves.

    Concretely: the fast-forward's discarded occurrences are REAL. They sit
    before ``start``, but generation's catch-up loop has no lower bound and
    materialises every one of them, incrementing ``occurrences_elapsed`` as it
    goes. A series with 2 instalments left whose frontier is 2 periods before
    ``start`` therefore has ZERO occurrences inside the window — it spends its
    last two getting there. Budgeting only the collect loop below reports 2
    in-window occurrences that generation will never create, and
    ``forecast_net`` moves across a generation run: precisely the TBD-260
    defect class.

    ⚠ Do not "simplify" this by clamping a negative budget to zero at the call
    site; see ``recurring_filters.remaining_occurrences``. The guard is
    ``<= 0``, never ``!= 0``.
    """
    out: list[datetime.date] = []
    d = next_due
    remaining = budget

    # FAST-FORWARD. Every pass DISCARDS the occurrence at ``d`` — and discarded
    # is not the same as never-existed: generation materialises it. So it costs
    # the series one instalment, exactly as an in-window occurrence does.
    while d < start:
        if remaining is not None:
            if remaining <= 0:
                return out
            remaining -= 1
        nxt = advance_date(d, freq)
        if nxt <= d:
            return out
        d = nxt

    # COLLECT.
    iterations = 0
    while d <= end:
        if remaining is not None and remaining <= 0:
            break
        if iterations >= max_iterations:
            break
        iterations += 1
        if remaining is not None:
            remaining -= 1
        out.append(d)
        nxt = advance_date(d, freq)
        if nxt <= d:
            break
        d = nxt

    return out
