"""Billing period service — manage explicit billing periods.

Periods are explicit records: each has a start_date, and an optional
end_date (null = currently open). Closing a period sets its end_date
and opens a new period starting the next day.

The org's billing_cycle_day is used as a hint to auto-create the first
period, but the user has full control over when to close.

Note on :func:`reanchor_period_dependents`: it has **no production caller**.
TBD-239 deleted the one it had (``PUT /billing-cycle``'s re-anchor), and its
named future consumers are TBD-235 (boundary editor / re-anchor as an
explicit confirmed action) and TBD-241 (``close_period`` bound). It is kept
deliberately, with its direct service tests. Two consequences: do not prune
it as dead code, and do not read those tests as end-to-end coverage — no
request path reaches it today.
"""

import calendar
import datetime
from dataclasses import dataclass
from typing import Literal

from dateutil.relativedelta import relativedelta
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import BillingPeriod
from app.models.budget import Budget
from app.models.category import Category
from app.models.user import Organization
from app.services.exceptions import ConflictError, NotFoundError, ValidationError


def _snap_to_cycle(d: datetime.date, cycle_day: int) -> datetime.date:
    """Pin date d to cycle_day within its month, clamping to month length."""
    last = calendar.monthrange(d.year, d.month)[1]
    return d.replace(day=min(cycle_day, last))


def current_cycle_window(
    cycle_day: int, today: datetime.date
) -> tuple[datetime.date, datetime.date]:
    """Billing cycle window [start, end_inclusive] containing `today`.

    Derived purely from billing_cycle_day — no DB I/O, no BillingPeriod row.
    start = most recent occurrence of cycle_day on/before today.
    end   = day before the next cycle start.
    """
    start = _snap_to_cycle(today, cycle_day)
    if start > today:
        start = _snap_to_cycle(today - relativedelta(months=1), cycle_day)
    next_start = _snap_to_cycle(start + relativedelta(months=1), cycle_day)
    return start, next_start - datetime.timedelta(days=1)


def next_cycle_window(
    cycle_day: int, today: datetime.date
) -> tuple[datetime.date, datetime.date]:
    """The billing cycle window [start, end_inclusive] AFTER the one
    containing `today` — i.e. the org's next upcoming cycle.

    Pure, no DB I/O. Re-derives the current window from `today` on every
    call (same self-correcting property as `current_cycle_window`), so there
    is no cumulative drift, and `_snap_to_cycle` clamps to month length
    (e.g. cycle_day=31 lands on Feb 28/29). Boundaries are inclusive and
    gap-free with the following cycle.
    """
    cur_start, _ = current_cycle_window(cycle_day, today)
    next_start = _snap_to_cycle(cur_start + relativedelta(months=1), cycle_day)
    following = _snap_to_cycle(next_start + relativedelta(months=1), cycle_day)
    return next_start, following - datetime.timedelta(days=1)


async def get_current_period(db: AsyncSession, org_id: int) -> BillingPeriod:
    """Get the currently open period. If none exists, auto-create one."""
    result = await db.execute(
        select(BillingPeriod).where(
            BillingPeriod.org_id == org_id,
            BillingPeriod.end_date.is_(None),
        ).order_by(BillingPeriod.start_date.desc())
    )
    open_periods = list(result.scalars().all())

    if len(open_periods) > 1:
        import structlog
        logger = structlog.stdlib.get_logger()
        await logger.awarning(
            "multiple open billing periods",
            org_id=org_id,
            count=len(open_periods),
            period_ids=[p.id for p in open_periods],
        )

    period = open_periods[0] if open_periods else None

    if period is None:
        # Auto-create first period based on org's billing_cycle_day
        org = await db.scalar(select(Organization).where(Organization.id == org_id))
        cycle_day = org.billing_cycle_day if org else 1

        today = datetime.date.today()
        start, _ = current_cycle_window(cycle_day, today)

        period = BillingPeriod(org_id=org_id, start_date=start)
        db.add(period)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            period = await db.scalar(
                select(BillingPeriod).where(
                    BillingPeriod.org_id == org_id,
                    BillingPeriod.start_date == start,
                )
            )
            if period is None:
                raise RuntimeError(
                    f"Billing period for org {org_id} vanished after IntegrityError"
                )
        await db.refresh(period)

    return period


async def resolve_period(
    db: AsyncSession, org_id: int, period_start: datetime.date | None,
) -> BillingPeriod:
    """Resolve a billing period by start_date, or fall back to the current open period.

    Raises ValidationError if period_start is given but no matching period exists.
    """
    if period_start:
        # Implemented ON TOP of `_find_period_by_start` (TBD-240 N7) rather
        # than carrying a fourth copy of the same SELECT. The only difference
        # between the two is the no-match behaviour: this raises, the helper
        # returns None — which is exactly why D4's callers cannot use this one.
        period = await _find_period_by_start(db, org_id, period_start)
        if period is None:
            raise ValidationError("Billing period not found")
        return period
    return await get_current_period(db, org_id)


async def list_periods(db: AsyncSession, org_id: int) -> list[BillingPeriod]:
    result = await db.execute(
        select(BillingPeriod)
        .where(BillingPeriod.org_id == org_id)
        .order_by(BillingPeriod.start_date.desc())
        .limit(24)
    )
    return list(result.scalars().all())


async def ensure_future_periods(
    db: AsyncSession, org_id: int, count: int = 3,
) -> list[BillingPeriod]:
    """Create stub periods for the `count` cycles after the OPEN period.

    Anchored to the open period's ``start_date`` (the ``base`` local below),
    not to today.
    For an org whose open period is months behind the calendar the stubs are
    therefore historic; correcting the anchor is TBD-235 blocker 1, not this
    function's job. Calling this repeatedly is idempotent: a candidate whose
    window intersects an existing period is skipped.
    """
    current = await get_current_period(db, org_id)
    org = await db.scalar(select(Organization).where(Organization.id == org_id))
    cycle_day = org.billing_cycle_day if org else 1

    # Build the target months: 1, 2, ... count months from current period
    base = current.start_date
    created = []
    for i in range(1, count + 1):
        next_start = _snap_to_cycle(base + relativedelta(months=i), cycle_day)
        end_date = _snap_to_cycle(next_start + relativedelta(months=1), cycle_day) - datetime.timedelta(days=1)

        # Skip when the candidate collides with an existing period
        # (TBD-239 §2). BOTH arms below are needed; neither is a superset of
        # the other.
        #
        # Arm 1 — exact start. Cheap, and the ONLY arm that is safe against
        # a concurrent `close_period`. `close_period` does not insert when a
        # stub already sits at its `new_start`; it REVIVES that stub by
        # setting `end_date = None`. So a row that arm 2 matched a moment
        # ago (closed, intersecting) can become an open row at a start this
        # loop is still about to propose. Arm 2 alone would then miss it and
        # `db.add` a duplicate, and the resulting IntegrityError surfaces
        # from autoflush inside the NEXT iteration's `db.scalar` — outside
        # the try/except around `db.commit()` below, so it escapes as a 500.
        # Arm 1 matches regardless of `end_date` and closes that window.
        #
        # ⚠ That argument depends on `close_period` never MOVING a
        # `start_date`. Arm 1 matches on exact start, so it can only cover the
        # window a revive opens if the revived row keeps the start it already
        # had. TBD-241's clamp changes WHICH row `close_period` revives but not
        # THAT it revives, and it moves no `start_date` — that prohibition is
        # one of its two load-bearing invariants (spec §1, §4). Any future
        # design that moves a `start_date` breaks arm 1 and must re-derive this
        # comment rather than assume it still holds.
        #
        # Arm 2 — window intersection. Exact-start matching alone was not
        # enough either: `PUT /billing-cycle` used to move the open period
        # off the grid and the cycle day can change at any time, so the very
        # next mount of Budgets or Forecasts proposed a whole second grid
        # whose windows sat across the existing one. Days counted twice, in
        # two periods.
        #
        # Arm 2 is compared against the RAW `end_date`, deliberately.
        # `effective_end`, `COALESCE(end_date, '9999-12-31')` or hydrating
        # the rows and filtering in Python would each make the OPEN period
        # (end_date IS NULL) intersect every candidate and stop stub
        # creation for every org — silently, since the loop just creates
        # nothing.
        #
        # `end_date IS NOT NULL` in arm 2 is redundant: in SQL three-valued
        # logic `end_date >= :start` already does not match NULL. It is kept
        # as documentation of intent. Letting arm 2 skip open rows is safe
        # because candidates are `base + i months` snapped to the cycle day
        # for `i >= 1` with `cycle_day` in [1, 28] (see
        # `BillingCycleUpdate` in schemas/settings.py), so a candidate always
        # lands in a strictly later calendar month than the open row it was
        # derived from; a backward overlap is impossible. That argument
        # depends on `base` coming from the MAX open start, which is what
        # `get_current_period`'s `order_by(start_date.desc())` guarantees
        # when the duplicate-open-row case warned about below is live.
        #
        # Known hole: this is blind to a SECOND open row whose start is not
        # a candidate start. `get_current_period` warns when it finds
        # several, and `POST /billing-period` can insert an open row at an
        # arbitrary start (`seed.py:260-261` does).
        # `uq_billing_period_org_start` backstops only exact-start
        # collisions.
        overlapping = await db.scalar(
            select(BillingPeriod.id).where(
                BillingPeriod.org_id == org_id,
                or_(
                    BillingPeriod.start_date == next_start,
                    and_(
                        BillingPeriod.end_date.is_not(None),
                        BillingPeriod.start_date <= end_date,
                        BillingPeriod.end_date >= next_start,
                    ),
                ),
            ).limit(1)
        )
        if overlapping is not None:
            # Skip silently and keep going: this runs on every Budgets and
            # Forecasts mount (budgets/page.tsx:97,
            # ForecastPlansClient.tsx:290), so a 409 here would break two
            # pages for an org whose roster is merely off-grid.
            import structlog
            await structlog.stdlib.get_logger().awarning(
                "billing.stub.skipped_overlap",
                org_id=org_id,
                candidate_start=next_start.isoformat(),
                candidate_end=end_date.isoformat(),
                existing_period_id=overlapping,
            )
            continue

        stub = BillingPeriod(org_id=org_id, start_date=next_start, end_date=end_date)
        db.add(stub)
        created.append(stub)

    if created:
        from sqlalchemy.exc import IntegrityError
        try:
            await db.commit()
            for s in created:
                await db.refresh(s)
        except IntegrityError:
            # Concurrent request already created the stubs — safe to ignore
            await db.rollback()
            created = []

    return created


async def reanchor_period_dependents(
    db: AsyncSession,
    *,
    org_id: int,
    old_start: datetime.date,
    new_start: datetime.date,
    new_end: datetime.date | None,
) -> int:
    """Move budgets anchored to ``old_start`` onto ``new_start`` / ``new_end``.

    Returns the number of budget rows re-anchored. Raises ConflictError
    (code ``budget_period_conflict``) when a budget already exists for the
    same category at ``new_start``.

    A boundary move in TBD-235 calls this TWICE: once for the previous
    period (old_start == new_start, only ``new_end`` changes) and once for
    the next period (old_start != new_start).

    Does **not** commit. ``Budget.period_start`` is the sole join key
    (``budget_service.list_budgets``, budget_service.py:129-130), so this
    write must land in the same transaction as the period write that moved
    the boundary; a crash between the two orphans every budget for that
    period and ``list_budgets`` then silently returns ``[]``.

    Three rules here are load-bearing (spec
    ``2026-07-27-billing-period-truth-and-safety.md`` section 4):

    1. **Identity case.** When ``old_start == new_start`` and ``new_end`` is
       already what every affected row carries, return 0 without running
       any pre-flight. The rows being "moved" ARE the rows at the
       destination, so a naive pre-flight finds each budget conflicting
       with itself, and the caller gets a 409 on what is really a no-op.
       TBD-239 deleted the one caller that reached this shape in
       production (``PUT /billing-cycle``'s re-anchor), so the rule is
       currently proven only by this module's own tests. It is kept
       because the named future callers hit the same shape by
       construction: TBD-235's boundary editor calls this twice per move
       and the first call is always ``old_start == new_start`` with only
       ``new_end`` changing.
    2. **The pre-flight excludes rows whose ``period_start == old_start``** —
       i.e. the rows being moved — for the same reason.
    3. **The IntegrityError backstop stays alongside the pre-flight.** A
       pre-flight SELECT alone is TOCTOU; under real MySQL two admins (or
       an admin and ``BillingCloseJob``, which ticks every 900s with
       ``automate_billing_close`` on by default) can race between the
       SELECT and the UPDATE and 500 anyway. The house pattern in this
       area is both (see :func:`get_current_period`, :func:`close_period`).
    """
    moving_at_old = (
        Budget.org_id == org_id,
        Budget.period_start == old_start,
    )
    update_where = moving_at_old

    if old_start == new_start:
        # Identity case — nothing moves horizontally. The only possible work
        # is refreshing the `period_end` snapshot, so if that is already
        # correct on every affected row this is a genuine no-op.
        if new_end is None:
            end_differs = Budget.period_end.is_not(None)
        else:
            end_differs = or_(
                Budget.period_end.is_(None), Budget.period_end != new_end
            )
        stale = await db.scalar(
            select(Budget.id).where(*moving_at_old, end_differs).limit(1)
        )
        if stale is None:
            return 0
        # Narrow the UPDATE to the rows whose snapshot is actually stale.
        # Unscoped it matches every row at `old_start`, including ones that
        # already carry `new_end`, and the returned count over-reports.
        # TBD-239 removed the `budgets_reanchored` audit key along with the
        # `PUT /billing-cycle` re-anchor, so nothing surfaces that count
        # today; keep it honest for the callers TBD-235/TBD-241 add back.
        update_where = (*moving_at_old, end_differs)

    # Pre-flight for uq_budget_org_cat_period. Excludes the rows being moved
    # (rule 2) — with old_start == new_start the two period_start predicates
    # are contradictory, so this correctly finds nothing.
    # `.correlate(None)` is load-bearing: the subquery's only FROM is
    # `budgets`, which is also the enclosing query's FROM. Pinning it off
    # keeps SQLAlchemy from ever auto-correlating it into a per-row
    # comparison against the outer Budget.
    moving_categories = (
        select(Budget.category_id)
        .where(*moving_at_old)
        .correlate(None)
        .scalar_subquery()
    )
    clashing = (
        await db.execute(
            select(Category.name)
            .select_from(Budget)
            .join(Category, Category.id == Budget.category_id)
            .where(
                Budget.org_id == org_id,
                Budget.period_start == new_start,
                Budget.period_start != old_start,
                Budget.category_id.in_(moving_categories),
            )
            .order_by(Category.name)
        )
    ).scalars().all()
    if clashing:
        raise ConflictError(
            "A budget already exists at the new period start for: "
            + ", ".join(clashing),
            code="budget_period_conflict",
        )

    try:
        result = await db.execute(
            update(Budget)
            .where(*update_where)
            .values(period_start=new_start, period_end=new_end)
        )
    except IntegrityError:
        # TOCTOU backstop (rule 3): someone inserted a budget at the
        # destination between our SELECT and this UPDATE.
        await db.rollback()
        raise ConflictError(
            "A budget already exists at the new period start",
            code="budget_period_conflict",
        )

    # `rowcount` is -1 when the driver cannot report a count; never let that
    # reach the audit detail as a negative number of budgets.
    return max(result.rowcount or 0, 0)


async def _next_period_start(
    db: AsyncSession, org_id: int, *, after: datetime.date
) -> datetime.date | None:
    """MIN(start_date) among the org's periods with ``start_date > after``.

    TBD-241 D9. Deliberately has **no upper-bound parameter**: `close_period`
    applies its `s0 <= close_date` test at the call site, which leaves this
    helper in the unbounded form TBD-240 needs for `effective_end`.

    Uses ``db.execute``, never ``db.scalar`` — pinned by the spec (§2, §5).
    The compiled statement contains both ``billing_periods`` and
    ``start_date``, so a shape-keyed test patch aimed at the existence check in
    :func:`_apply_close_step` would otherwise fire here instead and silently
    disable the coverage that patch exists to create.
    """
    result = await db.execute(
        select(func.min(BillingPeriod.start_date)).where(
            BillingPeriod.org_id == org_id,
            BillingPeriod.start_date > after,
        )
    )
    return result.scalar_one_or_none()


async def _find_period_by_start(
    db: AsyncSession, org_id: int, start: datetime.date
) -> BillingPeriod | None:
    """The org's period row at exactly ``start``, or ``None``.

    TBD-240 D4. ``scalar_one_or_none`` is safe: ``uq_billing_period_org_start``
    makes ``(org_id, start_date)`` unique.

    Exists as its own function because D4's two callers
    (``budget_service.update_budget`` / ``transfer_budget``) must NOT use
    :func:`resolve_period` — that raises ``ValidationError`` when no row
    matches, which would turn a ``PUT /budgets/{id}`` on a budget whose period
    row is missing into a 400 — nor :func:`get_current_period`, which
    auto-creates *and commits* a period row as a side effect of a plain read.
    :func:`resolve_period` is implemented on top of this (N7).

    ``db.execute``, never ``db.scalar`` (D7): the house convention for anything
    selecting ``BillingPeriod``, so that a future caller moved into
    ``close_period``'s path cannot consume the shape-keyed one-shot test patch
    that :func:`_next_period_start` and :func:`_lock_period` are pinned against.
    """
    return (
        await db.execute(
            select(BillingPeriod).where(
                BillingPeriod.org_id == org_id,
                BillingPeriod.start_date == start,
            )
        )
    ).scalar_one_or_none()


async def period_effective_end(
    db: AsyncSession, org_id: int, period: BillingPeriod
) -> datetime.date | None:
    """The period's roster-derived end. **No clock.**

    TBD-240 §2.2. Three cases, in order:

    * ``end_date IS NOT NULL`` → returned **verbatim**. A closed period's end
      is settled fact.
    * open, with a later period on the roster → the day before that period
      starts, so the two windows abut without overlapping.
    * open, with nothing later (``s0 is None``) → ``None``, meaning genuinely
      unbounded. This is the roster tail: there is no later period a
      future-dated settled row could belong to, so bounding it would hide
      spend and buy nothing. ⚠ This is a **knowingly retained** unbounded
      window, not a compatibility win — see :func:`period_spend_window_end`
      for what it costs.

    ⚠ **Reachable in production, transitively.** No caller invokes this
    directly, but :func:`period_spend_window_end` does (see its first line),
    and that helper has six live production call sites
    (``budget_service.py:142,194,242,355``, ``forecast_plan_service.py:322``,
    ``budget_rebalance_service.py:540``). ⚠ This paragraph replaces an
    earlier one claiming the helper had **no production caller at all**; that
    claim was false and had been repeated since #589 merged. It has never been
    at prune risk.

    ⚠ **Directly exercised as a differential oracle.** TBD-234a's
    :func:`kernel_derived_end` re-derives these exact three cases in memory,
    over a complete roster, without calling this function — and
    ``tests/services/test_period_anomalies.py``'s test 11 asserts the two
    agree row by row. That test is the only fence against the kernel drifting
    onto :func:`period_spend_window_end`'s semantics, which would make it
    paint phantom overlaps between the open row's floored window and the
    historic stubs on every lapsed org. So: do not prune this as dead code,
    do not collapse the two helpers into one, and do not "simplify" the
    kernel by making it call this instead — the differential is the point.

    Two prohibitions carried over from the boundary model
    (``reference_billing_period_boundary_model.md``). They apply to
    :func:`period_spend_window_end` equally, and are **restated in full there**
    rather than only cross-referenced — that is the helper with production
    callers, and the one a future author is likeliest to read in isolation.
    Keep the two copies in sync:

    1. **Never persist either result** to ``BillingPeriod.end_date`` or
       ``Budget.period_end``. Both columns mean "what the period's end *was*",
       written by ``close_period``; freezing a derived value into them would be
       a new lie in the same column (TBD-240 D5).
    2. **Never** use either in :func:`ensure_future_periods`' arm-2
       intersection predicate or :func:`_apply_close_step`'s straddle
       predicate. Both depend on raw ``end_date`` three-valued logic;
       substituting a derived end there makes the open row intersect every
       candidate and stops stub creation for **every** org, silently.
    """
    if period.end_date is not None:
        return period.end_date
    s0 = await _next_period_start(db, org_id, after=period.start_date)
    if s0 is None:
        return None
    return s0 - datetime.timedelta(days=1)


async def period_spend_window_end(
    db: AsyncSession,
    org_id: int,
    period: BillingPeriod,
    *,
    today: datetime.date | None = None,
) -> datetime.date | None:
    """:func:`period_effective_end`, then floored at ``today`` — **iff the
    period is open**. The upper bound every SPEND query must use.

    TBD-240 §2.2 / §2.3. The three clauses below are each load-bearing:

    * **Closed rows are returned verbatim, before any floor.** Flooring a
      closed period's end would silently re-open reported history — the worst
      outcome available here.
    * **``None`` stays ``None``.** The roster tail keeps its unbounded window,
      **knowingly**, and this is a residual rather than a win. Say it plainly:
      the very behaviour TBD-240 calls a defect is left in place for tail
      periods, so an org's ``spent`` depends on whether the stub roster has
      been materialized at all — and nothing on the read path materializes it.
      Stubs appear only when somebody triggers ``ensure_future_periods`` from
      elsewhere (the admin-only ``POST /settings/billing-periods/ensure-future``,
      a user copying budgets or a plan forward, or a ``close_period`` run). Two
      orgs with identical transactions can therefore report different ``spent``
      purely because one of them once clicked something. Accepted because
      bounding the tail would hide spend with no later period to move it to;
      revisit when the roster is guaranteed converged (TBD-241 /
      ``BillingCloseJob``). See :func:`period_effective_end`.
    * **``max(e, today)`` on the open interior.** ``ensure_future_periods``
      anchors its stubs on the open period's ``start_date``, not on today, and
      concedes in its own docstring that they are "therefore historic" for a
      lapsed org. So the *derived* end of a months-stale open row lands in the
      **past**, and every settled transaction dated after it — hand-entered,
      bank-imported (``import_service`` builds a ``TransactionCreate`` whose
      ``status`` defaults to settled), or generated by an auto-settle template
      — would fall outside the open period's window while the stub that
      contains it is rendered read-only by ``budgets/page.tsx``. The org's
      current spending would become invisible and unbudgetable: an under-count
      strictly worse than the over-count this ticket removes. The floor holds
      the line at today.

    **Accepted residual, recorded so it is not "fixed" by deleting the floor.**
    On a lapsed roster the floored window ``[start, today]`` still overlaps the
    historic stubs, and the double-counted region is **not just today** — it is
    the whole interval ``[derived_end + 1, today]``, which widens by one day
    per day for as long as the roster stays unconverged (spec §2.3). That
    double count happens today already and *unbounded*; this bounds it, and it
    disappears once the roster converges (``BillingCloseJob``, per TBD-241). An
    unconverged roster genuinely has ambiguous ownership of that interval; the
    right answer is to keep it visible in the editable row, not to make it
    invisible everywhere. Repairing the roster itself is **TBD-235 blocker 1**.

    ``today`` is keyword-only and injectable (D6) because this introduces the
    wall clock into money computation; tests anchor relative to
    ``date.today()`` rather than on literals near the clamp boundary
    (``reference_wall_clock_date_bomb_tests``). **Callers that resolve a window
    AND do any other date arithmetic must resolve the clock once themselves and
    pass a concrete date to both** — see :func:`suggest_rebalance
    <app.services.budget_rebalance_service.suggest_rebalance>`, where letting
    the ``None`` default through to two callees separated by a round-trip would
    reintroduce exactly the two-clocks straddle D6 exists to prevent.

    **The two prohibitions from :func:`period_effective_end` apply here
    verbatim, and they are restated rather than cross-referenced because this
    is the helper with production callers** (boundary model:
    ``reference_billing_period_boundary_model.md``):

    1. **Never persist this result** to ``BillingPeriod.end_date`` or
       ``Budget.period_end``. Both columns mean "what the period's end *was*",
       written by ``close_period``; freezing a derived, clock-dependent value
       into them would be a new lie in the same column (TBD-240 D5).
    2. **Never** use this in :func:`ensure_future_periods`' arm-2 intersection
       predicate or :func:`_apply_close_step`'s straddle predicate. Both depend
       on raw ``end_date`` three-valued logic; substituting a derived end there
       makes the open row intersect every candidate and stops stub creation for
       **every** org, silently.
    """
    end = await period_effective_end(db, org_id, period)
    if period.end_date is not None:
        return end
    if end is None:
        return None

    today = today if today is not None else datetime.date.today()
    window_end = max(end, today)

    # §2.4 — the non-inversion invariant. `_next_period_start` selects
    # `start_date > after` STRICTLY, so `end = s0 - 1 >= period.start_date`
    # and `max(end, today) >= end >= start`. The window can never invert.
    #
    # This is not decorative: `_apply_close_step` may legally leave the open
    # row starting TOMORROW (a close with `close_date = today`), so
    # `today < period.start_date` is a reachable state and must not be allowed
    # to produce `end < start`.
    #
    # `raise`, not `assert` — bare asserts are stripped under `python -O` and
    # this is money math. `RuntimeError`, never `ValidationError`: mapping this
    # to a 400 would turn an internal invariant violation into a user-facing
    # error on `GET /api/v1/budgets`.
    #
    # ⚠ Be honest about what this is: the branch is UNREACHABLE by
    # construction, given the two facts above, and **no test drives it** —
    # spec §5 test 5 exercises the tomorrow-start shape and asserts the window
    # does NOT invert, i.e. it proves the invariant holds, it does not cover
    # this `raise`. So the message string and the branch itself are unproven
    # code kept as a tripwire for a future change that breaks the derivation
    # (e.g. making `_next_period_start` non-strict). Do not read the absence of
    # coverage as a gap to close with a contrived test; read it as the reason
    # not to put anything load-bearing inside this branch.
    if window_end < period.start_date:
        raise RuntimeError(
            f"Spend window inverted for billing period {period.id}: "
            f"end {window_end} precedes start {period.start_date}"
        )
    return window_end


async def _lock_period(db: AsyncSession, period_id: int) -> BillingPeriod | None:
    """Re-select one period row ``FOR UPDATE`` and repopulate it.

    TBD-241 code review, finding F1. ``uq_billing_period_org_start`` only
    serialises writers that compute the **same** ``new_start``, and two closers
    routinely compute different ones: the scheduler passes ``boundary - 1``
    while a UI close passes yesterday. When the two differ, both closes commit,
    both succeed, no ``IntegrityError`` is ever raised, D4 never runs, and the
    org is left with an overlap plus two open rows. Locking the open row makes
    every writer serialise on it regardless of the date each computed.

    ``populate_existing=True`` enforces the codebase invariant that every FOR
    UPDATE refreshes the ORM identity-map entry with the locked row state
    (`transaction_service.get_account_for_update`, `budget_service`'s transfer
    lock). Returns ``None`` when the row is gone.

    ⚠ ``db.execute``, never ``db.scalar`` — for the same reason
    :func:`_next_period_start` is pinned that way. ``select(BillingPeriod)``
    compiles to a statement containing both ``billing_periods`` and
    ``start_date``, so a shape-keyed test patch aimed at
    :func:`_apply_close_step`'s existence check would fire here instead and
    silently disable the coverage that patch exists to create.

    ``FOR UPDATE`` is silently dropped by the SQLite dialect, so the in-memory
    fixtures behave exactly as before; the serialisation is real on MySQL.
    """
    return (
        await db.execute(
            select(BillingPeriod)
            .where(BillingPeriod.id == period_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _period_after_racer_close(
    db: AsyncSession, org_id: int, *, closed: BillingPeriod
) -> BillingPeriod:
    """The open row a racing closer left behind (D4 step 4).

    A racer that closed ``closed`` left its new open row at
    ``closed.end_date + 1 day`` — exact regardless of how the racer clamped.
    Return it without writing anything and without re-validating the caller's
    requested date: the racer's own D5 ran in its own transaction. Re-deriving
    the close date here is what made an earlier revision answer 400 for a close
    that had in fact succeeded.

    This rests on an invariant, not merely on today's code: **no writer may set
    ``end_date`` on a row without leaving a row at ``end_date + 1 day``.** Today
    only :func:`close_period` sets ``end_date`` on an existing row
    (``POST /billing-period`` only INSERTs).

    Reached from two places, which is why it is a function: D4's recovery, and
    the F1 lock in :func:`close_period` when the locked row comes back already
    closed. **Neither writes anything**, which is why the scheduler's
    ``closed_ids`` out-parameter stays untouched on this path (D10 defines it as
    the rows whose ``end_date`` *this run* wrote).
    """
    racer_start = closed.end_date + datetime.timedelta(days=1)
    racer_period = (
        await db.execute(
            select(BillingPeriod).where(
                BillingPeriod.org_id == org_id,
                BillingPeriod.start_date == racer_start,
            )
        )
    ).scalar_one_or_none()
    if racer_period is None or racer_period.end_date is not None:
        # Returning a closed row here would make the route reply
        # `{"end_date": None}` for a period that is closed.
        raise RuntimeError(
            f"Billing period at {racer_start} is missing or not open "
            f"after a concurrent close of period {closed.id}"
        )
    return racer_period


async def _apply_close_step(
    db: AsyncSession,
    org_id: int,
    current: BillingPeriod,
    requested: datetime.date,
) -> BillingPeriod:
    """Apply ONE close: clamp, re-anchor, close, revive-or-insert, commit.

    TBD-241 §2. This helper is deliberately **non-recursive** and contains no
    recovery: it ends at a bare ``db.commit()`` and lets ``IntegrityError``
    propagate to :func:`close_period`, which owns the try/except and the D4
    recovery. An earlier revision put the recovery inside this helper, which
    made it call itself — unbounded recursion under sustained contention, and
    "a second IntegrityError propagates" became unexpressible.

    ⚠ **The operation order below is normative, not illustrative.**
    ``async_session`` leaves autoflush ON (`database.py:89` sets only
    ``expire_on_commit=False``), so any statement issued while a
    ``BillingPeriod`` INSERT is pending flushes that INSERT, and a
    ``uq_billing_period_org_start`` violation then surfaces from wherever that
    statement happens to sit. Concretely, D5 (step e) must run BEFORE any
    pending INSERT. Two things go wrong if it does not, and **neither is "the
    IntegrityError escapes close_period's try"** — that try wraps the *entire*
    ``_apply_close_step`` call, so an autoflush from anywhere in steps a-i is
    caught and drives D4. *(An earlier revision of this docstring, and of the
    test that fences it, claimed otherwise; corrected in the TBD-241 code
    review, finding F5.)* What actually bites:

    1. :func:`reanchor_period_dependents` carries its **own** backstop at the
       shared-tail UPDATE. An autoflushed ``IntegrityError`` there is caught,
       ``db.rollback()`` **discards the whole close**, and the caller is handed
       ``ConflictError("budget_period_conflict")`` — a *budget* conflict
       announced for a concurrent *period* close, on a route that then answers
       409 instead of recovering.
    2. The **retry** invocation in ``close_period``'s ``except`` arm has no
       ``try`` around it (deliberately — the helper is non-recursive and a
       second ``IntegrityError`` is meant to propagate). A misplaced D5 that
       raises on the retry therefore escapes as a genuine unhandled 500.

    ``requested`` in, resolved out: the caller passes the requested date and
    the helper returns the row it opened, so D4's retry re-derives the clamp
    from scratch instead of re-clamping an already-clamped value.
    """
    import structlog
    logger = structlog.stdlib.get_logger()

    # ── step a — straddling rows (D12). Pure SELECT, no mutation. ─────────
    #
    # A row starting at or before `current.start_date` but ending after it is
    # a PRE-EXISTING overlap. It is excluded from clamp selection by
    # construction (candidates are `start_date > current.start_date`
    # *strictly*), which is what keeps the clamped date from ever dropping
    # below the lower bound — the specific hazard that killed the rejected 409
    # design. We log it and move on; repairing it is TBD-235.
    #
    # `end_date IS NOT NULL` is documentation of intent, not protection: in SQL
    # three-valued logic `end_date >= :start` already fails to match NULL. Kept
    # for the same reason `ensure_future_periods` arm 2 keeps its copy.
    straddling = (
        await db.scalars(
            select(BillingPeriod).where(
                BillingPeriod.org_id == org_id,
                BillingPeriod.id != current.id,
                BillingPeriod.end_date.is_not(None),
                BillingPeriod.start_date <= current.start_date,
                BillingPeriod.end_date >= current.start_date,
            ).order_by(BillingPeriod.start_date)
        )
    ).all()
    straddling_rows = [
        (p.id, p.start_date, p.end_date) for p in straddling
    ]

    # ── step b — the clamp target (D9) ───────────────────────────────────
    s0 = await _next_period_start(db, org_id, after=current.start_date)

    # ── step c — clamp iff the requested window would swallow s0 ─────────
    #
    # `new_start = resolved + 1 day`, so after clamping `new_start == s0`
    # EXACTLY and the existing exact-start revive below matches by
    # construction. That is the whole design: no step-selection rules, no
    # tie-break, no row selected by containment, and no `start_date` ever
    # moves.
    if s0 is not None and s0 <= requested:
        resolved = s0 - datetime.timedelta(days=1)
    else:
        resolved = requested

    # ── step b′ — what the REQUESTED window would have swallowed (D10) ────
    #
    # Counterfactual on purpose. After clamping, every other row starts at
    # `>= s0 > resolved`, so the set of rows inside the window actually closed
    # is always empty and would carry no information.
    absorbed_ids: list[int] = []
    if resolved != requested:
        absorbed_ids = list(
            (
                await db.scalars(
                    select(BillingPeriod.id)
                    .where(
                        BillingPeriod.org_id == org_id,
                        BillingPeriod.start_date > current.start_date,
                        BillingPeriod.start_date <= requested,
                    )
                    .order_by(BillingPeriod.start_date)
                )
            ).all()
        )

    # ── step d ────────────────────────────────────────────────────────────
    new_start = resolved + datetime.timedelta(days=1)

    # ── step e — D5: refresh the closing period's budget snapshot ─────────
    #
    # Identity re-anchor (old_start == new_start): only `period_end` moves.
    # `Budget.period_end` is a stored snapshot written as `period.end_date` at
    # creation, so a budget created while its period was open carries NULL
    # forever unless something refreshes it. This step is that refresh.
    #
    # ⚠ Updated by TBD-240. This used to say the stale NULL made
    # `_compute_spent` drop its upper bound for a period that is in fact
    # closed. That is no longer reachable on any live path: every spend query
    # now derives its bound from the PERIOD ROW via `period_spend_window_end`
    # (budget_service.py:85 is the guard it feeds), so a closed period is
    # bounded by its own `end_date` no matter what the budget snapshot says.
    # Two reasons the step is still required:
    #
    #   1. `Budget.period_end` is emitted verbatim in `BudgetResponse`
    #      (`budget_service._to_response`) and read by the frontend. A NULL
    #      there on a closed period is a user-visible lie regardless of which
    #      bound the sum used.
    #   2. It is the fallback bound for D4's stranded-budget branch in
    #      `update_budget` / `transfer_budget` — the one path where the
    #      snapshot is still authoritative because no period row was found.
    #
    # ⚠ `new_end` is `resolved`, NEVER the raw `close_date` parameter. That
    # parameter is `None` on every UI close (page.tsx sends no date), which
    # would drive the identity branch's `new_end is None` path and BLANK
    # `period_end` on every budget of the period being closed.
    #
    # Placement is load-bearing — see this function's docstring. Nothing
    # unique-violating is pending here: steps a-d issue no BillingPeriod
    # mutation at all.
    await reanchor_period_dependents(
        db,
        org_id=org_id,
        old_start=current.start_date,
        new_start=current.start_date,
        new_end=resolved,
    )

    # ── step f — the FIRST BillingPeriod mutation ────────────────────────
    # An UPDATE of `end_date`, which participates in no unique constraint, so
    # the autoflush at step g is harmless.
    current.end_date = resolved

    # ── step g — exact-start revive lookup ───────────────────────────────
    # If a row already exists at new_start (a stub from `ensure_future_periods`,
    # or the row the clamp deliberately targeted) revive it instead of
    # inserting a duplicate that would trip `uq_billing_period_org_start`.
    existing = await db.scalar(
        select(BillingPeriod).where(
            BillingPeriod.org_id == org_id,
            BillingPeriod.start_date == new_start,
        )
    )

    # ── step h — revive or insert ────────────────────────────────────────
    revived_id: int | None = None
    revived_previous_end: datetime.date | None = None
    if existing is not None:
        # CAPTURE FIRST. Nulling `end_date` is this design's one irreversible
        # write, and `revived_previous_end` is D10's load-bearing recovery key;
        # reading it after the assignment yields None and destroys exactly what
        # the key exists to preserve.
        revived_id, revived_previous_end = existing.id, existing.end_date
        existing.end_date = None
        # D5, second call: budgets created against a stub carry that stub's old
        # non-NULL `period_end`, which is stale the moment the row is reopened
        # and is shipped to the client by `_to_response`. Safe here — only
        # UPDATEs are pending.
        await reanchor_period_dependents(
            db,
            org_id=org_id,
            old_start=new_start,
            new_start=new_start,
            new_end=None,
        )
        new_period = existing
    else:
        new_period = BillingPeriod(org_id=org_id, start_date=new_start)
        db.add(new_period)

    # ── step i — bare commit; IntegrityError propagates to close_period ──
    # NO statement may be inserted between step h and this commit: the INSERT
    # above is the only pending unique-constrained write.
    await db.commit()

    # ── step j — emit AFTER the commit ───────────────────────────────────
    # A pre-commit emit would describe a close that then rolled back. The
    # residual (process death between i and j) is accepted and recorded in the
    # spec.
    for period_id, straddler_start, straddler_end in straddling_rows:
        await logger.awarning(
            "billing.close.straddling_row_ignored",
            org_id=org_id,
            period_id=period_id,
            period_start=straddler_start.isoformat(),
            period_end=straddler_end.isoformat(),
            closing_period_start=current.start_date.isoformat(),
        )
    if resolved != requested:
        await logger.ainfo(
            "billing.close.clamped",
            org_id=org_id,
            requested_close_date=requested.isoformat(),
            clamped_to=resolved.isoformat(),
            absorbed_period_ids=absorbed_ids,
            revived_period_id=revived_id,
            revived_previous_end=(
                revived_previous_end.isoformat() if revived_previous_end else None
            ),
        )
    if revived_id is not None:
        # Its OWN event, not a key on `clamped`: the unclamped revive (the
        # ordinary stub case) overwrites `end_date` too and emits no `clamped`
        # event, so attaching the key there would lose it on the common path.
        await logger.ainfo(
            "billing.close.revived",
            org_id=org_id,
            revived_period_id=revived_id,
            revived_previous_end=(
                revived_previous_end.isoformat() if revived_previous_end else None
            ),
        )

    return new_period


async def close_period(
    db: AsyncSession,
    org_id: int,
    close_date: datetime.date | None = None,
    *,
    today: datetime.date | None = None,
    closed_ids: list[int] | None = None,
) -> BillingPeriod:
    """Close the current period and open a new one.

    ``close_date`` defaults to yesterday (salary came today, close yesterday).
    Returns the NEW (open) period.

    Performs **exactly one** close per call. Multi-cycle convergence for a
    lapsed org belongs to the caller: ``BillingCloseJob.run`` loops, the manual
    route does not (one click closes one period, and every close stays
    individually audited).

    ``today`` is keyword-only and threaded in by the scheduler (TBD-241 D2), so
    a tick that straddles midnight cannot have its own close date rejected by
    the D1 bound below. Two honest residuals: the manual path still reads
    ``date.today()`` here, and ``get_current_period``'s auto-create branch
    reads it independently, so ``today=`` is not authoritative when no open row
    exists.

    ``closed_ids`` is an optional **out-parameter**: when given, the id of the
    row this call actually closed is appended to it. D10 freezes the return
    type, and two paths return a perfectly good open row **without writing
    anything** (the F1 lock finding a row a racer already closed, and D4 step
    4). Without this the scheduler's convergence loop cannot tell a close it
    performed from a close it merely observed, and its ``closed_period_ids`` /
    ``steps`` audit keys over-report. Callers that do not care omit it.

    The step numbering below is normative — see :func:`_apply_close_step`.
    """
    current = await get_current_period(db, org_id)                          # 1

    today = today if today is not None else datetime.date.today()          # 2
    requested = (                                                          # 3
        close_date
        if close_date is not None
        else today - datetime.timedelta(days=1)
    )

    if requested > today:                                                  # 4 (D1)
        # Strict `>`, so "close yesterday" and "close today" both stay legal.
        # The frontend pins its error copy on this exact sentence
        # (`mapBillingPeriodCloseError`), so do not reword it casually.
        raise ValidationError("Close date cannot be in the future")

    current_id = current.id

    # ── F1 — serialise every closer on the open row ──────────────────────
    #
    # `uq_billing_period_org_start` alone does NOT close the concurrency hole:
    # it only collides writers that compute the SAME `new_start`, and the two
    # production callers routinely compute different ones (the scheduler passes
    # `boundary - 1`, a UI close passes yesterday). With org cycle_day 1, a
    # single open row `[2026-06-01, NULL)` and a clock of 2026-07-28, the
    # scheduler resolves `new_start = 2026-07-01` and a concurrent admin click
    # resolves `new_start = 2026-07-28`; both commit, neither raises, D4 never
    # runs, and the roster ends up `[06-01, 07-27]` overlapping `[07-01, NULL)`
    # plus `[07-28, NULL)` — with nothing logged. `main` corrupts identically,
    # but this ticket exists to stop `close_period` producing overlaps, and the
    # convergence loop widens the window from one transaction per tick to up to
    # `MAX_CONVERGENCE_STEPS`.
    #
    # The lock is taken here, before the roster is read and before anything is
    # decided, so it covers the whole read-decide-write sequence: it is released
    # only by `_apply_close_step`'s commit at step i (or by D4's rollback, which
    # re-takes it).
    current = await _lock_period(db, current_id)
    if current is None:
        # `org_data_service.py:144` deletes every BillingPeriod for an org, so
        # this is reachable, not theoretical.
        raise RuntimeError(f"Billing period {current_id} vanished before close")

    if current.end_date is not None:
        # A racer closed this period between `get_current_period` and the lock.
        # Same ruling as D4 step 4, and the same helper: return the racer's open
        # row, write nothing, and do NOT re-validate `requested` against it.
        return await _period_after_racer_close(db, org_id, closed=current)

    if requested < current.start_date:                                     # 5
        # After the lock, so it reads the locked row rather than a stale one.
        # `start_date` never moves, so the value is the same either way; the
        # ordering matters only because the racer branch above must not be
        # reached through a 400.
        raise ValidationError("Close date cannot be before the period start date")

    try:                                                                   # 6
        new_period = await _apply_close_step(db, org_id, current, requested)
    except IntegrityError:
        # ── D4: re-entrancy ──────────────────────────────────────────────
        await db.rollback()

        # Re-fetch the closing row BY ID, and re-take the F1 lock: the rollback
        # released it. Never `get_current_period` here: it auto-creates and
        # commits a period when none is open, and on a duplicate-open roster it
        # can return an EARLIER row and send the scheduler's convergence loop
        # backwards.
        current = await _lock_period(db, current_id)
        if current is None:
            # Same wipe as above, mid-flight. The previous code tolerated it
            # silently and fell through; make it loud instead — the router
            # audits it as a failure and re-raises.
            raise RuntimeError(
                f"Billing period {current_id} vanished after IntegrityError"
            )

        if current.end_date is not None:
            # A racer closed the SAME period. Return its open row untouched.
            return await _period_after_racer_close(db, org_id, closed=current)

        # Otherwise our own write lost to a peer INSERT at `new_start`. Run the
        # step exactly once more with the ORIGINAL requested date, which
        # re-derives the clamp and re-issues D5 (the rollback discarded its
        # UPDATE). The helper is non-recursive, so a second IntegrityError
        # propagates and is audited by the router's broad except.
        new_period = await _apply_close_step(db, org_id, current, requested)

    if closed_ids is not None:
        # Only here: every `return` above is a path that wrote nothing.
        closed_ids.append(current_id)

    await db.refresh(new_period)                                           # 7
    return new_period


# ═══════════════════════════════════════════════════════════════════════════
# TBD-234a — the billing period anomaly kernel
#
# Spec: `specs/2026-07-29-billing-period-roster-design.md` (revision 5), §2.2
# through §2.5. The seam that defines this section:
#
#     234a = "given a complete roster and a clock, what is wrong with it."
#     234b = "fetch the roster, aggregate the money, window the display,
#             render it."
#
# ⚠ **Zero window vocabulary crosses that line**, and that is the test for
# whether a future edit belongs here. Revisions 1 through 3 of the spec
# defined the kernel's input domain by the DISPLAY WINDOW rather than by the
# invariant being checked, and every finding of three rejection rounds fell
# out of that one fusion. Every property below (contiguity, non-overlap,
# exactly-one-open, straddling, lapsed) is a property of a WHOLE roster; a
# windowed sample of a roster is not a roster and does not carry them.
#
# ⚠ **NO PRODUCTION CALLER YET — do NOT prune any of this as dead code.**
# Every symbol below (`RosterRow`, `CompleteRoster`, `PeriodAnomaly`,
# `AnomalyKind`, `PeriodStatus`, `OVERLAP_ANALYSIS_CAP`,
# `OVERLAP_EMISSION_CAP`, `load_complete_roster`, `kernel_derived_end`,
# `period_status`, `find_period_anomalies`) has zero references outside this
# module today. That is by design and it is temporary: **TBD-234b** is the
# named consumer — it adds `GET /settings/billing-periods/roster`, the
# response model over `PeriodAnomaly`, and the page — and §8's split forbids
# the two shipping together. Same idiom, same reason, as
# :func:`reanchor_period_dependents` in the module docstring. Two
# consequences: do not prune, and do not read the kernel's direct service
# tests as end-to-end coverage — no request path reaches any of it today.
# ═══════════════════════════════════════════════════════════════════════════


#: §2.4a. `len(roster.rows) > OVERLAP_ANALYSIS_CAP` skips overlap analysis;
#: at exactly the cap the analysis RUNS. Only `overlap` is O(n²) — `gap` is
#: adjacent-pair O(n), `straddling` resolves ONE anchor and makes ONE O(n)
#: pass (§2.4a said `O(n·k)` in open rows; the implementation is better than
#: that advertisement and the comment is corrected here), and `inverted` /
#: `no_open` / `duplicate_open` are O(n) — so the refusal is scoped to that
#: one rule. Suppressing every structural marker would kill `duplicate_open`
#: on 1000+ row orgs, which is precisely where that corruption hides.
#: 2M pair comparisons is sub-second in Python and the real cliff is nearer
#: 5000, so this refusal path should never fire in practice. That is what a
#: refusal path should look like.
OVERLAP_ANALYSIS_CAP = 2000

#: §2.4a's emission ceiling. The analysis cap bounds comparison COST and says
#: nothing about emission COUNT: 1999 closed rows each spanning ten years is a
#: legal roster, sits below the cap, keeps the comparison loop sub-second, and
#: yields on the order of 2M `overlap` markers — a response in the hundreds of
#: megabytes and a page that cannot render. Admin-authenticated and
#: self-inflicted, so it is bounded rather than treated as a threat, and
#: bounded by the same named rule: truncation for analysis must be REFUSED,
#: never silently applied.
#:
#: ⚠ **The boundary is pinned, in the same direction as the analysis cap:**
#: at exactly `OVERLAP_EMISSION_CAP` candidate overlaps nothing is suppressed
#: and NO `overlap_emission_capped` marker is emitted; the marker fires only
#: at `cap + 1` candidates. Its payload carries `overlap_count`, the number of
#: `overlap` markers the roster WOULD have produced — never the number
#: emitted, which is the cap itself and therefore carries no information.
OVERLAP_EMISSION_CAP = 5000


#: §2.5's anomaly kinds, in the order the response list sorts by. ⚠ Named
#: `PeriodAnomaly`, never a bare `Anomaly`: `schemas/ai_forecast.py:38`
#: already owns `AnomalyFlag` in an unrelated AI-forecast sense, and a bare
#: `Anomaly` here would collide in every reader's head and in every grep.
AnomalyKind = Literal[
    "gap",
    "overlap",
    "duplicate_open",
    "no_open",
    "inverted",
    "straddling",
    "lapsed_open",
    "overlap_analysis_skipped",
    "overlap_emission_capped",
]

_KIND_ORDER: dict[str, int] = {
    "gap": 0,
    "overlap": 1,
    "duplicate_open": 2,
    "no_open": 3,
    "inverted": 4,
    "straddling": 5,
    "lapsed_open": 6,
    "overlap_analysis_skipped": 7,
    "overlap_emission_capped": 8,
}

#: Sort placeholder for markers carrying no `from_date`. Bound at import time,
#: on purpose: the kernel must not touch `datetime.date` at call time, or
#: test 14a's `_ExplodingDate` monkeypatch would have nothing to prove.
_SORT_EPOCH = datetime.date(1, 1, 1)

#: §2.3's five-branch partition, evaluated in this order, first match wins.
PeriodStatus = Literal["invalid", "open", "upcoming", "current_by_calendar", "past"]


@dataclass(frozen=True)
class RosterRow:
    """One billing period, reduced to the three columns the kernel reads.

    ⚠ **A row tuple, NOT an ORM entity** (§2.2 amendment 1). Decoupling from
    :class:`~app.models.billing.BillingPeriod` is what makes
    :func:`load_complete_roster`'s unbounded fetch trivial on a 10k-row org,
    and what keeps the kernel free of session state.
    """

    id: int
    start_date: datetime.date
    end_date: datetime.date | None


@dataclass(frozen=True)
class CompleteRoster:
    """**Every** period row an org has, `start_date` ASC. §2.2.

    The name carries a precondition, and the precondition is the whole
    contract: `rows` is the org's COMPLETE roster, not a window, not a page,
    not a sample. :func:`kernel_derived_end`'s equality with
    :func:`period_effective_end` holds **only** under it, because
    `rows[i + 1].start_date` is `MIN(start_date) WHERE start_date >
    rows[i].start_date` only when nothing is missing between them.

    ⚠ **A pure kernel can never verify this itself.** Completeness is a claim
    about rows that are NOT in the list, so purity and self-verification are
    mutually exclusive: no in-kernel assertion, length check or invariant
    guard can work, and anyone reaching for one has misread the problem.
    Enforcement is therefore at CONSTRUCTION — :func:`load_complete_roster`
    is the only site in `backend/app/` allowed to build one, audited by
    `tests/test_complete_roster_single_construction_site.py`'s AST guard.

    ⚠ That guard is the precondition's **only** mechanism. This repository
    has no type checker (round-4 finding F3), and a frozen dataclass has a
    public `__init__`, so both `CompleteRoster(org_id=1, rows=tuple(windowed))`
    and `dataclasses.replace(roster, rows=windowed)` succeed at runtime with
    nothing else objecting.
    """

    org_id: int
    rows: tuple[RosterRow, ...]


@dataclass(frozen=True)
class PeriodAnomaly:
    """One marker. §2.5's nine kinds, one payload shape per kind.

    Optional fields are populated per kind and left `None` otherwise:

    ==========================  ==================================================
    kind                        fields
    ==========================  ==================================================
    `gap`                       `from_period_id`, `to_period_id`, `from_date`,
                                `to_date` — the UNCOVERED interval itself, both
                                bounds inclusive
    `overlap`                   `from_period_id`, `to_period_id`, `from_date` =
                                `rows[j].start_date`, `to_date` = the **LEFT**
                                row's derived end (not the intersection)
    `duplicate_open`            `period_ids` — every open row's id, ASC. Ids,
                                never a count
    `no_open`                   `period_ids`, always empty; present for schema
                                uniformity
    `inverted`                  `period_id`
    `straddling`                `period_id`, `anchor_period_id`
    `lapsed_open`               `period_id`, `effective_end` (which is `< today`)
    `overlap_analysis_skipped`  `period_count`, `cap`
    `overlap_emission_capped`   `overlap_count`, `cap` — `overlap_count` is
                                the number of `overlap` markers the roster
                                WOULD have produced, always `> cap` when this
                                marker fires. ⚠ Never "how many were emitted":
                                that is the cap, by construction, so a field
                                carrying it could never carry information
    ==========================  ==================================================

    A frozen dataclass with a `Literal` tag, matching the service-layer
    convention (`cc_cycle_service.py:31`, `budget_rebalance_service.py:112`,
    `loan_service.py:103`). The discriminated Pydantic union is the house
    pattern at the WIRE boundary only (`schemas/dashboard.py:96-141,173`), and
    that is 234b's response model, built over these dataclasses without the
    kernel importing Pydantic at all.
    """

    kind: AnomalyKind
    from_period_id: int | None = None
    to_period_id: int | None = None
    period_id: int | None = None
    period_ids: tuple[int, ...] | None = None
    anchor_period_id: int | None = None
    from_date: datetime.date | None = None
    to_date: datetime.date | None = None
    effective_end: datetime.date | None = None
    period_count: int | None = None
    overlap_count: int | None = None
    cap: int | None = None


async def load_complete_roster(db: AsyncSession, org_id: int) -> CompleteRoster:
    """Fetch **every** period row the org has, `start_date` ASC. §2.2.

    ⚠ **The only constructor of :class:`CompleteRoster` in `backend/app/`**,
    and deliberately the dullest function in this module: one `SELECT`, no
    LIMIT, no date predicate, **no branches**. Every one of those absences is
    load-bearing, because the type's precondition is exactly the claim that
    nothing was filtered out. A future edit that adds a window here does not
    narrow a query, it silently converts every anomaly the kernel reports
    into a claim about a sample.

    Selects the three columns, never the entity: the kernel reads no other
    column, and `RosterRow` tuples keep an unbounded fetch cheap on a
    thousand-row org.

    ⚠ Do **not** route this through :func:`list_periods`: that caps at 24 rows
    and answers a different question. (Cited by SYMBOL, not by line: this
    module's own line numbers shift under every edit above, and a stale
    self-citation is residual R1's defect class.)

    Index note, stated rather than left to be discovered: `ix_billing_periods_org`
    is `(org_id)` only (`014_billing_periods.py`), with no `start_date`
    component, so the `ORDER BY` filesorts. On a roster of hundreds of narrow
    rows that is cheap; no index change is proposed, and this note exists so a
    later reader does not read the omission as an oversight.
    """
    result = await db.execute(
        select(
            BillingPeriod.id,
            BillingPeriod.start_date,
            BillingPeriod.end_date,
        )
        .where(BillingPeriod.org_id == org_id)
        .order_by(BillingPeriod.start_date)
    )
    return CompleteRoster(
        org_id=org_id,
        rows=tuple(
            RosterRow(id=row.id, start_date=row.start_date, end_date=row.end_date)
            for row in result
        ),
    )


def kernel_derived_end(roster: CompleteRoster, i: int) -> datetime.date | None:
    """:func:`period_effective_end`'s three cases, in memory. **No clock.**

    ::

        end(rows, i) = rows[i].end_date                if end_date IS NOT NULL
                     = rows[i+1].start_date - 1 day    if open and i+1 exists
                     = None                            if open and i is the tail

    This is an **equality** with the async helper, not an approximation, and
    it holds only under :class:`CompleteRoster`'s precondition.
    `BillingPeriod` carries `uq_billing_period_org_start`
    (`models/billing.py:12-14`; MySQL side `017_billing_period_unique_constraint.py:24`),
    so on a complete `start_date`-ASC list `rows[i+1].start_date` **is**
    `MIN(start_date) WHERE start_date > rows[i].start_date`, which is exactly
    what :func:`_next_period_start` computes. The three branches are
    line-for-line :func:`period_effective_end`'s three-case body (cited by
    symbol, not by line — an in-file line citation goes stale on the next
    edit above it, which is residual R1's defect class).

    ⚠ **Case-split on `end_date IS NULL` or the whole kernel dies silently.**
    Revision 1 of the spec applied the successor rule to EVERY row; composed
    with the gap rule that made `row.end ≡ successor.start - 1` identically,
    so `successor.start > row.end + 1` reduced to `successor.start >
    successor.start` — false for every row on every roster, and the gap and
    overlap detectors would have shipped as dead code. The fence against that
    is the differential test (test 11), never an IO mandate: in-memory
    derivation was never the defect.

    ⚠ **Module-level, and it takes no `today`.** Both are contract, frozen in
    §8.1. A closure inside :func:`find_period_anomalies` is not reachable from
    test 11, which asserts on this function by name; and a `today` parameter
    is the one-line door to `period_spend_window_end`'s floored semantics
    (`max(end, today)`), which would paint phantom overlaps between the open
    row's floored window and the historic stubs on every lapsed org. The async
    helper being structurally unreachable from a sync sessionless kernel is a
    fact about the FUNCTION, not about the SEMANTICS.
    """
    row = roster.rows[i]
    if row.end_date is not None:
        return row.end_date
    if i + 1 < len(roster.rows):
        return roster.rows[i + 1].start_date - datetime.timedelta(days=1)
    return None


def _is_inverted(row: RosterRow) -> bool:
    """`end_date IS NOT NULL AND end_date < start_date`. §2.3 branch 1.

    ⚠ **One definition, two callers**, and it is deliberately NOT routed
    through :func:`period_status`. `period_status` needs a clock;
    "inverted" is a purely STRUCTURAL property of one row, and
    :func:`find_period_anomalies` uses it to suppress `gap` and `overlap`.
    Deriving a clock-free structural suppression rule from a clock-dependent
    classifier would make the structural output set depend on `today` —
    exactly the fusion §2.4's two output sets exist to keep apart.

    It is extracted rather than written twice because the two copies were
    the same expression and the kernel's gap/overlap suppression depends on
    them staying the same expression.
    """
    return row.end_date is not None and row.end_date < row.start_date


def period_status(row: RosterRow, *, today: datetime.date) -> PeriodStatus:
    """The canonical status of one row. §2.3. **`today` is required.**

    Five branches, evaluated in this order, first match wins. ⚠ **The order
    is normative**, not stylistic: revision 1 of the spec gave four unordered
    predicates that were not disjoint, and two implementers writing the
    `if/elif` in different orders would both have satisfied it while producing
    different canonical answers.

    1. `invalid`             `end_date IS NOT NULL AND end_date < start_date`
    2. `open`                `end_date IS NULL`
    3. `upcoming`            `start_date > today`
    4. `current_by_calendar` `start_date <= today <= end_date`
    5. `past`                `end_date < today`

    Four divergent definitions of "current" already exist in the frontend and
    this must not become a fifth: it is the field TBD-242 will point every
    site at. `current_by_calendar` is the disputed shape — the dashboard
    computes `isCurrent`/`isPast`/`isFuture` (`dashboard/page.tsx:271-273`)
    and such a row falls through all three, while Forecasts calls it
    *Current* (`ForecastPlansClient.tsx:253-256`). Naming it explicitly is
    what lets TBD-242 resolve that against a tested definition.

    ⚠ **What this does NOT do: it classifies rows, it does not SELECT one.**
    On a lapsed roster the open row is `open` while a stub is
    `current_by_calendar`; on a corrupt roster two rows can both be
    `current_by_calendar`. Choosing which row a screen shows is TBD-242's
    problem and is deliberately not pre-empted here.

    ⚠ **Branch 1 is UNREACHABLE through shipped code and stays anyway, as a
    DEFENSIVE branch.** Every `end_date` writer was enumerated and each is
    provably non-inverting (`BillingPeriodCreate`'s validator at
    `schemas/settings.py:32-35`; :func:`ensure_future_periods`' stub insert,
    `snap(next_start + 1mo) - 1`; :func:`_apply_close_step`'s `resolved`,
    guarded by :func:`close_period`'s `requested < current.start_date` check;
    three writers that write NULL). ⚠ Those three are cited by SYMBOL rather
    than by line on purpose: they all sit above this function in this same
    file, so a line citation goes stale the moment anything above it grows.
    This section shipped with three such citations already wrong — residual
    R1's own defect class, one commit later.

    But `models/billing.py:12-14` carries no CHECK
    constraint, so the database **will** accept an inverted row written by a
    direct DB edit, by operator prod access, or by a future writer that skips
    the schema layer. A diagnostic whose subject is data corruption must
    classify corruption it did not cause — and without branch 1 such a row
    matches **both** `upcoming` and `past`, breaking the partition.

    Never `date.today()` here (D8a, §8.1 item 3). The caller resolves the
    clock once and passes a concrete date to every callee, or a request
    straddling UTC midnight classifies a row against day D while computing its
    window against day D+1.
    """
    if _is_inverted(row):
        return "invalid"
    if row.end_date is None:
        return "open"
    if row.start_date > today:
        return "upcoming"
    if row.start_date <= today <= row.end_date:
        return "current_by_calendar"
    # By exhaustion the remainder is `end_date < today`: the row is closed,
    # non-inverted, started on or before today, and does not contain today.
    return "past"


def _anomaly_sort_key(anomaly: PeriodAnomaly) -> tuple:
    """§2.5's pinned ordering: kind (declaration order), then the period ids
    the marker references ASCENDING, then `from_date`.

    Markers referencing no id sort last within their kind. The leading
    comparison is still "the lowest period id the marker references", which is
    what §2.5 pins; the remaining ids are the tie-break behind it.

    ⚠ **The id component is the WHOLE sorted tuple, not `min(ids)`, and that
    is what makes the order total rather than merely deterministic.** The
    review of this PR proved the collapsed form ties: rows `A(id=5)`,
    `B(id=6)` and `C(id=1)`, with both `A` and `B` containing `C`, produce
    `overlap 5→1` and `overlap 6→1`, whose `min(ids)` is `1` for both — two
    distinct markers, byte-identical keys. Output stayed deterministic (a
    stable sort plus a fixed emission order), but §2.5 licenses tests to
    assert the list DIRECTLY on the strength of totality, and 234b will pin
    rendering order on it, so the claim is made true rather than softened.

    **What is guaranteed, exactly:** no two markers `find_period_anomalies`
    emits from the same roster share a key. Within a kind, each rule emits at
    most one marker per row (`inverted`, `straddling`), per adjacent pair
    (`gap`), per `i < j` pair (`overlap`), or per roster (`duplicate_open`,
    `no_open`, `lapsed_open`, and the two refusal markers), and `RosterRow.id`
    values are unique — so the sorted id tuple identifies the marker within
    its kind. The two refusal markers reference no id at all and are emitted
    at most once each, so they can only tie with themselves.

    Tuples of unequal length stay comparable because every element is an
    `int`: Python compares element-wise and falls back to length only on a
    common prefix. No padding is needed, and the empty tuple is reachable only
    when the preceding `0 if ids else 1` component already matched.
    """
    ids: list[int] = []
    for value in (
        anomaly.from_period_id,
        anomaly.to_period_id,
        anomaly.period_id,
        anomaly.anchor_period_id,
    ):
        if value is not None:
            ids.append(value)
    if anomaly.period_ids:
        ids.extend(anomaly.period_ids)
    return (
        _KIND_ORDER[anomaly.kind],
        0 if ids else 1,
        tuple(sorted(ids)),
        0 if anomaly.from_date is not None else 1,
        anomaly.from_date if anomaly.from_date is not None else _SORT_EPOCH,
    )


def find_period_anomalies(
    roster: CompleteRoster, *, today: datetime.date
) -> list[PeriodAnomaly]:
    """Everything wrong with an org's billing period roster. §2.4.

    **Pure, sync, no DB.** It takes no session, so :func:`period_effective_end`
    and :func:`period_spend_window_end` are both structurally unreachable from
    it, and it can be called by the route, by a future fleet-wide sweep script,
    or straight from a test with a hand-shaped roster.

    Nine markers, in two output sets, because one of them is not clock-free:

    * **structural** — `gap`, `overlap`, `duplicate_open`, `no_open`,
      `inverted`, `straddling`, plus the two refusal markers. No clock.
    * **temporal** — `lapsed_open`, which reads the injected `today`.

    Revision 1 of the spec put both in one undifferentiated set and then
    asserted the whole set was clock-independent, a direct contradiction,
    since "in the past" is a comparison against `today`.

    The rules, each with its domain pinned:

    * **`gap` is ADJACENT-PAIR.** An all-pairs gap rule is meaningless — every
      non-neighbour pair has something between them.
    * **`overlap` is ALL-PAIRS**, and the domain is normative. Adjacent-pair
      overlap detection is unsound: on `A[Jan 1 → Dec 31]`, `B[Feb 1 → Feb 28]`,
      `C[Mar 1 → Mar 31]` it reports one overlap where there are two and renders
      `C` clean — the nested-containment class `routers/settings.py:417-421`'s
      TOCTOU hole admits, missed on the page that exists to find it. This is
      the only O(n²) rule and §2.4a's cap is set where its cost stops being
      free.
    * **`straddling` is anchored on the open row :func:`get_current_period`
      would select**, the MAX-`start_date` row with `end_date IS NULL`. That is
      the row :func:`_apply_close_step` will actually evaluate, so the marker
      predicts real behaviour rather than a hypothetical. With **zero** open
      rows it is not computed at all: `no_open` already carries that signal and
      there is nothing to straddle.
    * **`straddling` excludes the anchor itself** (`i != anchor_index`).
      Without it, the anchor trivially straddles itself on this ticket's own
      healthy shape. The shipped precedent is exact:
      :func:`_apply_close_step`'s straddle query carries `BillingPeriod.id !=
      current.id` and has always excluded the anchor from its own straddle
      set. (By symbol, not by line — see :func:`period_status` on why in-file
      line citations are banned in this section.)
    * **`straddling` is emitted IN ADDITION TO `overlap`, never instead of
      it.** Suppressing the overlap would hide genuine overlaps on any roster
      containing a straddler — precisely the rosters this exists for.

    Two suppression rules, both normative:

    * ⚠ **NO predicate anywhere may compare a `None` derived end.** A row whose
      derived end is `None` (the roster tail) is never the LEFT member of a
      pair; it may be the RIGHT member of either rule, because both rules read
      only the LEFT row's end and excluding it on the right would suppress real
      gaps measured against a tail open row. `straddling` and `lapsed_open` are
      not pair rules and carry their own guards: on `[…closed…, OPEN]`, the
      fleet's commonest roster because nothing on the read path materialises
      stubs, an unguarded predicate evaluates `None >= date(...)` and 500s.
    * **`gap` and `overlap` are SUPPRESSED on `invalid` rows, as either
      member.** An inverted row's end precedes its own start, so the pair to
      its left overlaps and the pair to its right gaps, neither of which
      describes anything a reader can act on. `inverted` carries the signal.

    A consequence worth stating because it looks like a bug and is not: the
    open row can never gap or overlap against its *immediate* successor — its
    end is defined by that successor, so they abut by construction. It can
    still overlap a non-adjacent row. That is why `[…closed…, OPEN, stub,
    stub]` yields no structural markers.
    """
    rows = roster.rows
    n = len(rows)
    anomalies: list[PeriodAnomaly] = []

    ends = [kernel_derived_end(roster, i) for i in range(n)]
    # ⚠ Same predicate as `period_status` branch 1, and it is SHARED rather
    # than duplicated (:func:`_is_inverted`): the gap/overlap suppression
    # below depends on the two staying the same expression.
    invalid = [_is_inverted(row) for row in rows]

    # ── inverted (§2.3 branch 1 / §2.4 shape 5) ──────────────────────────
    for i, row in enumerate(rows):
        if invalid[i]:
            anomalies.append(PeriodAnomaly(kind="inverted", period_id=row.id))

    # ── gap — ADJACENT pairs only ────────────────────────────────────────
    one_day = datetime.timedelta(days=1)
    for i in range(n - 1):
        left_end = ends[i]
        if left_end is None or invalid[i] or invalid[i + 1]:
            continue
        if rows[i + 1].start_date > left_end + one_day:
            anomalies.append(
                PeriodAnomaly(
                    kind="gap",
                    from_period_id=rows[i].id,
                    to_period_id=rows[i + 1].id,
                    from_date=left_end + one_day,
                    to_date=rows[i + 1].start_date - one_day,
                )
            )

    # ── overlap — ALL pairs, and the only rule the two caps touch ────────
    if n > OVERLAP_ANALYSIS_CAP:
        # Refuse, loudly. ⚠ Never return an empty list when analysis was
        # skipped: the skipped marker is itself an anomaly, and every O(n)
        # rule above and below still ran.
        anomalies.append(
            PeriodAnomaly(
                kind="overlap_analysis_skipped",
                period_count=n,
                cap=OVERLAP_ANALYSIS_CAP,
            )
        )
    else:
        # ⚠ The scan runs to completion even once the emission ceiling is hit,
        # and the early `break` it replaces was a false economy. Stopping
        # early is what made the old `emitted_count` payload incapable of
        # carrying information: it was always exactly the cap, so the marker
        # rendered "5000 of 5000". `overlap_count` is the count the roster
        # WOULD have produced, which is the number an operator staring at a
        # corrupt roster actually needs. The extra cost is bounded by the
        # analysis cap directly above — `n <= OVERLAP_ANALYSIS_CAP` means at
        # most ~2M pair comparisons, the exact budget that cap was sized for.
        emitted = 0
        overlap_count = 0
        for i in range(n):
            left_end = ends[i]
            if left_end is None or invalid[i]:
                continue
            for j in range(i + 1, n):
                if invalid[j]:
                    continue
                if rows[j].start_date > left_end:
                    continue
                overlap_count += 1
                if emitted >= OVERLAP_EMISSION_CAP:
                    continue
                anomalies.append(
                    PeriodAnomaly(
                        kind="overlap",
                        from_period_id=rows[i].id,
                        to_period_id=rows[j].id,
                        from_date=rows[j].start_date,
                        to_date=left_end,
                    )
                )
                emitted += 1
        # Boundary pinned, same direction as the analysis cap: at exactly the
        # ceiling nothing was suppressed, so there is nothing to refuse.
        if overlap_count > OVERLAP_EMISSION_CAP:
            anomalies.append(
                PeriodAnomaly(
                    kind="overlap_emission_capped",
                    overlap_count=overlap_count,
                    cap=OVERLAP_EMISSION_CAP,
                )
            )

    # ── the open rows: duplicate_open / no_open / straddling / lapsed_open
    #
    # ⚠ All four are computed from the COMPLETE roster like every other
    # marker. Revision 3 of the spec passed the open rows in as a separate
    # `open_row_ids` argument — an org-wide carve-out that existed only
    # because the rest of the kernel was windowed. Org-wide is the general
    # rule now and the carve-out is deleted.
    open_indexes = [i for i in range(n) if rows[i].end_date is None]

    if not open_indexes:
        # Every consumer calls `get_current_period`, which would auto-create
        # AND COMMIT a row here. This marker is how the roster says so
        # without one being manufactured.
        anomalies.append(PeriodAnomaly(kind="no_open", period_ids=()))
    else:
        if len(open_indexes) > 1:
            # The most damaging shape: every frontend
            # `findIndex(p => p.end_date === null)` silently picks the first,
            # so two rows both claim "current" and different screens can pick
            # differently. Ids, never a count — the page must be able to name
            # them.
            anomalies.append(
                PeriodAnomaly(
                    kind="duplicate_open",
                    period_ids=tuple(rows[i].id for i in open_indexes),
                )
            )

        # `rows` is `start_date` ASC, so the last open index IS the MAX-start
        # open row — the one `get_current_period` selects.
        anchor_index = open_indexes[-1]
        anchor = rows[anchor_index]

        for i in range(n):
            if i == anchor_index:
                continue
            end = ends[i]
            # ⚠ CURRENTLY UNREACHABLE, and kept as defence in depth. Proven by
            # review: deleting it leaves every test green. `kernel_derived_end`
            # returns `None` only for the roster TAIL; an open tail row is by
            # definition the MAX-start open row, so it IS the anchor, and the
            # `i == anchor_index` skip above has already consumed it. Do NOT
            # "repair" a test to reach this line, and do NOT delete it: it
            # becomes live the moment the anchor rule stops selecting the
            # MAX-start open row, and F1(b) is what an unguarded `None >= date`
            # costs (a 500 on the fleet's commonest roster).
            if end is None:
                continue
            if rows[i].start_date <= anchor.start_date and end >= anchor.start_date:
                anomalies.append(
                    PeriodAnomaly(
                        kind="straddling",
                        period_id=rows[i].id,
                        anchor_period_id=anchor.id,
                    )
                )

        # Temporal. An open TAIL row has no derived end, so it has no end that
        # can be in the past and there is nothing to report — and comparing
        # its `None` against `today` would raise.
        anchor_end = ends[anchor_index]
        if anchor_end is not None and anchor_end < today:
            anomalies.append(
                PeriodAnomaly(
                    kind="lapsed_open",
                    period_id=anchor.id,
                    effective_end=anchor_end,
                )
            )

    anomalies.sort(key=_anomaly_sort_key)
    return anomalies
