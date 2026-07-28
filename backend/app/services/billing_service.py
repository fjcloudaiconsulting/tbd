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
from sqlalchemy import or_, select, update
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

    Anchored to the open period's ``start_date``, not to today (line 161).
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

        # Skip when the candidate window [next_start, end_date] intersects an
        # existing period (TBD-239 §2). Exact-start matching was not enough:
        # `PUT /billing-cycle` used to move the open period off the grid and
        # the cycle day can change at any time, so the very next mount of
        # Budgets or Forecasts proposed a whole second grid whose windows sat
        # across the existing one. Days counted twice, in two periods.
        #
        # Compared against the RAW `end_date`, deliberately. `effective_end`,
        # `COALESCE(end_date, '9999-12-31')` or hydrating the rows and
        # filtering in Python would each make the OPEN period (end_date IS
        # NULL) intersect every candidate and stop stub creation for every
        # org — silently, since the loop just creates nothing.
        #
        # `end_date IS NOT NULL` is redundant: in SQL three-valued logic
        # `end_date >= :start` already does not match NULL. It is kept as
        # documentation of intent. Excluding open rows is safe because
        # candidates are `base + i months` snapped to the cycle day for
        # `i >= 1` with `cycle_day` in [1, 28] (schemas/settings.py:12), so a
        # candidate always lands in a strictly later calendar month than the
        # open row it was derived from; a backward overlap is impossible.
        #
        # Known hole: this is blind to a SECOND open row.
        # `get_current_period` warns when it finds several, and
        # `POST /billing-period` can insert an open row at an arbitrary start
        # (`seed.py:250-251` does). `uq_billing_period_org_start` backstops
        # only exact-start collisions.
        overlapping = await db.scalar(
            select(BillingPeriod.id).where(
                BillingPeriod.org_id == org_id,
                BillingPeriod.end_date.is_not(None),
                BillingPeriod.start_date <= end_date,
                BillingPeriod.end_date >= next_start,
            ).limit(1)
        )
        if overlapping:
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
       with itself. This is not hypothetical: ``close_period`` defaults to
       "close yesterday", so any org that ever closed manually has an open
       period starting off the cycle-day grid, and an admin re-saving the
       same cycle day would get a permanent 409 on a previously working
       no-op. ``./pfv seed`` hits the same path ~7 days a month.
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
        # already carry `new_end`, and the returned count (surfaced as
        # `budgets_reanchored` in the audit detail) over-reports.
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


async def close_period(db: AsyncSession, org_id: int, close_date: datetime.date | None = None) -> BillingPeriod:
    """Close the current period and open a new one.
    close_date defaults to yesterday (salary came today, close yesterday).
    Returns the NEW (open) period."""
    current = await get_current_period(db, org_id)

    if close_date is None:
        close_date = datetime.date.today() - datetime.timedelta(days=1)

    if close_date < current.start_date:
        raise ValidationError("Close date cannot be before the period start date")

    new_start = close_date + datetime.timedelta(days=1)
    current_id = current.id

    current.end_date = close_date

    # If a future stub already exists at new_start (created by ensure_future_periods),
    # revive it as the open period instead of inserting a duplicate that would trip
    # the (org_id, start_date) unique constraint.
    existing = await db.scalar(
        select(BillingPeriod).where(
            BillingPeriod.org_id == org_id,
            BillingPeriod.start_date == new_start,
        )
    )
    if existing is not None:
        existing.end_date = None
        new_period = existing
    else:
        new_period = BillingPeriod(org_id=org_id, start_date=new_start)
        db.add(new_period)

    try:
        await db.commit()
    except IntegrityError:
        # Race: a concurrent request inserted (org_id, new_start) between our
        # SELECT and our INSERT. Roll back, re-fetch the winning row, revive it,
        # and re-apply the close on the previous period — making close_period
        # idempotent under concurrency (mirrors get_current_period/ensure_future_periods).
        await db.rollback()
        current = await db.scalar(
            select(BillingPeriod).where(BillingPeriod.id == current_id)
        )
        if current is not None and current.end_date is None:
            current.end_date = close_date
        new_period = await db.scalar(
            select(BillingPeriod).where(
                BillingPeriod.org_id == org_id,
                BillingPeriod.start_date == new_start,
            )
        )
        if new_period is None:
            raise RuntimeError(
                f"Billing period at {new_start} vanished after IntegrityError"
            )
        new_period.end_date = None
        await db.commit()

    await db.refresh(new_period)
    return new_period
