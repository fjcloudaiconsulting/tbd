"""Shared date utilities for recurring-frequency date advancement."""

import datetime

from dateutil.relativedelta import relativedelta

from app.models.recurring import Frequency

# Defensive per-RUN cap on ``recurring_service``'s catch-up loop, which
# aliases this value as ``MAX_CATCHUP_ITERATIONS``.
#
# ⚠ **Nothing in this module consumes it any more.** It used to also bound
# ``occurrences_in_window``'s collect loop, and the two were one number on the
# claim that "the two walks over a template's occurrence grid are sized by one
# number rather than by two constants that drift apart". TBD-286 removed the
# collect loop's cap outright (see that docstring), so the projection no longer
# truncates at all and there is nothing left here to keep in step with
# generation. It lives on in this module only because
# ``recurring_service.MAX_CATCHUP_ITERATIONS`` imports it and an AST fence
# (``test_forecast_overdue_recurring.test_f17a_...``) pins that binding to an
# alias rather than a re-declared literal. Retiring the alias — moving the
# literal into ``recurring_service`` beside ``_MAX_FRONTIER_ADVANCE_STEPS``,
# whose comment already argues that the alias rule is only about "walks that
# must TRUNCATE TOGETHER" — is TBD-338, not this ticket. That cleanup is
# precisely the mutant F17a's AST guard exists to kill, so it needs the fence
# re-aimed in the same change and cannot be a drive-by.
#
# Generation's cap is legitimate where a projection's was not: it bounds WORK
# and MAKES PROGRESS (it mutates ``next_due_date`` forward, so the next run
# resumes further along), and it LOGS (``recurring.generate.catchup_cap``).
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


def _next_occurrence(
    d: datetime.date, freq: Frequency
) -> datetime.date | None:
    """``advance_date(d, freq)``, or ``None`` when there is no next occurrence.

    The two ways a grid ends, folded into one place so both loops below treat
    them identically:

    * **No progress.** ``advance_date`` returning ``<= d`` would spin the walk
      forever. This is the real defence a cap was standing in for, and it is
      the one the fast-forward has relied on alone since PR 599.
    * **Past ``datetime.date.max``.** ``advance_date`` has no next value to
      give, and it does not fail cleanly: ``timedelta`` addition raises
      ``OverflowError`` (weekly, biweekly) while ``relativedelta`` raises
      ``ValueError`` (monthly, quarterly, yearly). Catching only one of the two
      leaves three frequencies unguarded, so both are caught.

      An ``end`` in year 9999 is admin-reachable — ``POST
      /api/v1/settings/billing-period`` validates ordering and overlap but not
      span (``schemas/settings.py``), and it is the shape a "no end" sentinel
      typo takes. With the collect loop capped at 500 the walk stopped long
      before the ceiling and this never fired; uncapping it without this guard
      would turn ``GET /api/v1/forecast`` into an unhandled 500 rather than the
      truncation TBD-286 removed. Terminating is also the *correct* answer, not
      merely a safe one: there is no occurrence after ``date.max``, and ``end``
      can never exceed it, so nothing in ``[start, end]`` is being dropped.
    """
    try:
        nxt = advance_date(d, freq)
    except (OverflowError, ValueError):
        return None
    return nxt if nxt > d else None


def occurrences_in_window(
    next_due: datetime.date,
    freq: Frequency,
    start: datetime.date,
    end: datetime.date,
    *,
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
    strictly forward for every frequency, ``_next_occurrence``'s no-progress
    guard is the real defence against a runaway, and the loop terminates at
    ``start``. ``forecast_plan_service.populate_from_sources`` already ships
    exactly this uncapped shape.

    **The COLLECT loop carries no iteration budget either, since TBD-286.** It
    kept one — ``max_iterations``, 500 — on the argument that its exposure was
    bounded by the WINDOW rather than by a user-supplied date, and that the
    window comes from the billing-period roster. The premise was false. The
    roster is not a bound: ``POST /api/v1/settings/billing-period`` takes
    ``start_date`` and ``end_date`` straight from an admin request body, and
    ``BillingPeriodCreate`` validates only their ORDER (plus an overlap check
    in the router, which bounds position, not length). A closed period spanning
    decades is one accepted request, and ``compute_forecast`` reads
    ``period.end_date`` verbatim into ``window_end``. Reaching >500 occurrences
    of one template therefore needs no corruption, just a long period row.

    Once past it the failure was the ticket's own defect class: the projection
    truncated in silence — no log, no marker, just a smaller number — while
    ``generate_due_transactions``, capped per RUN but advancing its frontier
    every run, materialised every occurrence across successive ticks. So
    ``forecast_net`` moved with no user action, exactly as the fast-forward's
    cap made it move.

    Raising the constant was never the fix: it moves the boundary and keeps the
    failure mode. What is left instead is the loop's natural bound — it
    terminates at ``end``, and ``_next_occurrence`` above closes the two ways
    the walk could fail to terminate. That the natural bound is enough was
    MEASURED rather than assumed, at the genuinely widest walk the type system
    permits — ``datetime.date.min`` (0001-01-01) to ``datetime.date.max``
    (9999-12-31), which no ``DATE`` column can even hold (MySQL's floor is
    1000-01-01):

    * weekly: 521,723 occurrences, 0.18s, **21.4MB peak**;
    * monthly: 119,988 occurrences, 0.24s, **4.9MB peak**.

    ⚠ Those are ``tracemalloc`` PEAK figures, and they are the honest ones. An
    earlier revision of this comment quoted 3.7MB / 0.8MB, which is
    ``sys.getsizeof(out)`` over the narrower 1900→9998 walk: the LIST OBJECT's
    pointer array only, EXCLUDING the ``datetime.date`` objects it points at.
    The dates are ~4.7x the pointers, so that number understates the real cost
    by that factor. Do not "recorrect" the peak back down to it.

    There is no unbounded hang to trade a wrong number for. §11 of
    ``specs/2026-07-30-forecast-overdue-recurring-design.md`` pre-registered
    this removal: *"If a period roster ever admits multi-year windows, delete
    the budget"*.

    ⚠ What is NOT fixed here, and is a separate ticket (TBD-335): an absurd
    window still makes ``account_balance_forecast_service`` emit one response
    line per projected occurrence. Bounding a billing period's SPAN at its
    writer is the honest place for that, and it is a product decision about the
    limit. Deferred because such a bound needs ``start_date`` bounded too (an
    OPEN row at 2000-01-01 with a successor in 2026 is a 26-year window on its
    own) and because it does not repair rows already stored. It is NOT deferred
    on the ground that "a bound tight enough to matter would bite below 500 and
    re-create this defect at a lower boundary" — that argument was made and is
    unsound. A span bound is a 400 at the WRITER: the over-long window never
    exists, so nothing truncates and nothing is hidden. This defect was a SILENT
    TRUNCATION at the READER. And ``generate_due_transactions`` materialises on
    ``current_cycle_window(cycle_day, today)``, which is roster-independent, so
    bounding a period's span cannot move what generation creates — the
    conservation property is not in play at all.

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
        nxt = _next_occurrence(d, freq)
        if nxt is None:
            return out
        d = nxt

    # COLLECT.
    while d <= end:
        if remaining is not None and remaining <= 0:
            break
        if remaining is not None:
            remaining -= 1
        out.append(d)
        nxt = _next_occurrence(d, freq)
        if nxt is None:
            break
        d = nxt

    return out
