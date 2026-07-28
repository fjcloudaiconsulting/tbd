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
        result = await db.execute(
            select(BillingPeriod).where(
                BillingPeriod.org_id == org_id,
                BillingPeriod.start_date == period_start,
            )
        )
        period = result.scalar_one_or_none()
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
    (budget_service.py:100-101), so this write must land in the same
    transaction as the period write that moved the boundary; a crash
    between the two orphans every budget for that period and
    ``list_budgets`` then silently returns ``[]``.

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
    # forever and `_compute_spent` then drops its upper bound
    # (budget_service.py:62-63) for a period that is in fact closed.
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
