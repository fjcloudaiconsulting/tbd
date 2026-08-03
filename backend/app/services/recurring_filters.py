"""Filters and predicates expressing instalment-series exhaustion (TBD-275).

Mirrors ``transaction_filters``' idiom: one module owning one predicate, so the
five places that read recurring templates cannot each grow their own spelling
of "is this series still running?".

**Exhaustion is DERIVED, never written.** An exhausted series keeps
``is_active = True`` and keeps its row. Three reasons, in order of severity:

1. ``stop_recurring`` NULLs ``recurring_id`` on EVERY surviving transaction the
   template produced. Calling it on exhaustion would destroy exactly the
   grouping this feature exists to create -- a finished 12-month instalment
   plan would lose the link joining its 12 rows the instant the 12th landed.
   ``delete_recurring`` is worse: it removes the intent record entirely.
2. ``is_active`` is USER intent ("I paused this"). ``occurrence_count`` is a
   different user intent ("this runs 12 times"). Collapsing the second onto the
   first makes them indistinguishable in the UI and makes resume ambiguous:
   re-activating an exhausted template would have to guess whether the user
   wanted more instalments or just un-paused a finished plan.
3. A written flag is state that can disagree with the arithmetic. Derived, it
   cannot.

**The arithmetic is defined exactly once**, in ``remaining_occurrences``, and
``active_series_filter`` is its SQL twin. Any read site that needs one needs
both: the filter drops series that are ALREADY exhausted at query time, and the
Python guard stops a walk that exhausts DURING it. Neither subsumes the other.
"""
from sqlalchemy import or_

from app.models.recurring import RecurringTransaction


def active_series_filter():
    """SQL clause: templates that may still deliver at least one occurrence.

    ``is_active`` AND not exhausted. Applied at every site that reads templates
    to project or materialise occurrences -- ``forecast_service``,
    ``recurring_service.generate_due_transactions``,
    ``forecast_plan_service.populate_from_sources``,
    ``scenario_engine.build_state`` and the ``recurring_generation`` scheduler
    job's ``is_due``. The scheduler one is not optional: without it an exhausted
    template reports "there is work" on every tick forever, and the job wakes,
    generates nothing, and returns a no-op run for the life of the org.

    ``occurrence_count IS NULL`` is the open-ended case and must short-circuit
    the comparison: in SQL ``x < NULL`` is NULL, not TRUE, so a bare
    ``occurrences_elapsed < occurrence_count`` silently drops EVERY open-ended
    template -- i.e. every template that existed before TBD-275.
    """
    return (RecurringTransaction.is_active == True) & or_(  # noqa: E712
        RecurringTransaction.occurrence_count.is_(None),
        RecurringTransaction.occurrences_elapsed
        < RecurringTransaction.occurrence_count,
    )


def remaining_occurrences(r: RecurringTransaction) -> int | None:
    """How many occurrences this series may still deliver. ``None`` = unbounded.

    ⚠ **The result is NOT clamped at zero and must not be.** A downward edit
    (``occurrence_count`` 5 -> 2 with 3 already elapsed) yields ``-1``, and the
    negative value is the honest answer: the series has over-delivered relative
    to its new intent. Clamping to 0 would hide that from every caller and, far
    worse, would make the difference between a ``> 0`` guard and a ``!= 0``
    guard UNOBSERVABLE -- both stop at exactly 0.

    Consequently **every consumer must test ``> 0``, never ``!= 0``**. With
    ``!= 0`` a negative remaining reads as "budget available" and each step
    takes it further from zero, so the guard never fires again: the series
    over-generates until some unrelated iteration cap happens to stop it.
    ``has_remaining_occurrences`` below is the one place that comparison is
    written for ORM rows; ``date_utils.occurrences_in_window``'s ``budget``
    is the one place it is written for the walkers.
    """
    if r.occurrence_count is None:
        return None
    return r.occurrence_count - (r.occurrences_elapsed or 0)


def has_remaining_occurrences(r: RecurringTransaction) -> bool:
    """True when the series may deliver at least one more occurrence.

    The in-loop counterpart to ``active_series_filter``. Sites that walk a
    template's grid need this as well as the filter, because a series that
    passes the filter can still exhaust part-way through the walk.
    """
    rem = remaining_occurrences(r)
    return rem is None or rem > 0
