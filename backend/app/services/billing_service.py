"""Billing period service — manage explicit billing periods.

Periods are explicit records: each has a start_date, and an optional
end_date (null = currently open). Closing a period sets its end_date
and opens a new period starting the next day.

The org's billing_cycle_day is used as a hint to auto-create the first
period, but the user has full control over when to close.
"""

import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import BillingPeriod
from app.models.user import Organization
from app.services.exceptions import ConflictError, NotFoundError, ValidationError


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
        y, m, d = today.year, today.month, today.day
        if d >= cycle_day:
            start = datetime.date(y, m, cycle_day)
        else:
            start = datetime.date(y, m - 1 if m > 1 else 12, cycle_day)
            if m == 1:
                start = datetime.date(y - 1, 12, cycle_day)

        period = BillingPeriod(org_id=org_id, start_date=start)
        db.add(period)
        await db.commit()
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
    """Create stub periods for the next `count` months from today.

    Always anchored to today — calling this multiple times is idempotent
    and will never create stubs beyond `count` months in the future.
    """
    import calendar

    from dateutil.relativedelta import relativedelta

    current = await get_current_period(db, org_id)
    org = await db.scalar(select(Organization).where(Organization.id == org_id))
    cycle_day = org.billing_cycle_day if org else 1

    def _snap_to_cycle(d: datetime.date) -> datetime.date:
        try:
            return d.replace(day=cycle_day)
        except ValueError:
            last = calendar.monthrange(d.year, d.month)[1]
            return d.replace(day=min(cycle_day, last))

    # Build the target months: 1, 2, ... count months from current period
    base = current.start_date
    created = []
    for i in range(1, count + 1):
        next_start = _snap_to_cycle(base + relativedelta(months=i))

        # Skip if already exists
        existing = await db.scalar(
            select(BillingPeriod.id).where(
                BillingPeriod.org_id == org_id,
                BillingPeriod.start_date == next_start,
            )
        )
        if existing:
            continue

        end_date = _snap_to_cycle(next_start + relativedelta(months=1)) - datetime.timedelta(days=1)

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


async def close_period(db: AsyncSession, org_id: int, close_date: datetime.date | None = None) -> BillingPeriod:
    """Close the current period and open a new one.
    close_date defaults to yesterday (salary came today, close yesterday).
    Returns the NEW (open) period."""
    current = await get_current_period(db, org_id)

    if close_date is None:
        close_date = datetime.date.today() - datetime.timedelta(days=1)

    if close_date < current.start_date:
        raise ValidationError("Close date cannot be before the period start date")

    current.end_date = close_date

    # Open new period starting the day after close
    new_period = BillingPeriod(
        org_id=org_id,
        start_date=close_date + datetime.timedelta(days=1),
    )
    db.add(new_period)
    await db.commit()
    await db.refresh(new_period)
    return new_period
