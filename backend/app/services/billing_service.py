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
        )
    )
    period = result.scalar_one_or_none()

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
    """Create stub periods for upcoming months so the user can plan ahead.

    Stubs have a start_date and end_date derived from the org's billing cycle day.
    They are distinguishable from real (closed) periods only by being in the future.
    Returns the newly created stubs (if any).
    """
    from dateutil.relativedelta import relativedelta

    current = await get_current_period(db, org_id)
    org = await db.scalar(select(Organization).where(Organization.id == org_id))
    cycle_day = org.billing_cycle_day if org else 1

    # Find the latest period start_date
    result = await db.execute(
        select(BillingPeriod.start_date)
        .where(BillingPeriod.org_id == org_id)
        .order_by(BillingPeriod.start_date.desc())
        .limit(1)
    )
    latest_start = result.scalar_one()

    created = []
    for i in range(count):
        # Next period starts ~1 month after the latest
        next_start = latest_start + relativedelta(months=1)
        # Snap to cycle_day
        try:
            next_start = next_start.replace(day=cycle_day)
        except ValueError:
            # e.g. cycle_day=31 in a 30-day month — use last day
            import calendar
            last_day = calendar.monthrange(next_start.year, next_start.month)[1]
            next_start = next_start.replace(day=min(cycle_day, last_day))

        # Check if it already exists
        existing = await db.scalar(
            select(BillingPeriod.id).where(
                BillingPeriod.org_id == org_id,
                BillingPeriod.start_date == next_start,
            )
        )
        if existing:
            latest_start = next_start
            continue

        # Compute end_date (day before the next cycle start)
        end_date = next_start + relativedelta(months=1)
        try:
            end_date = end_date.replace(day=cycle_day)
        except ValueError:
            import calendar
            last_day = calendar.monthrange(end_date.year, end_date.month)[1]
            end_date = end_date.replace(day=min(cycle_day, last_day))
        end_date = end_date - datetime.timedelta(days=1)

        stub = BillingPeriod(org_id=org_id, start_date=next_start, end_date=end_date)
        db.add(stub)
        created.append(stub)
        latest_start = next_start

    if created:
        await db.commit()
        for s in created:
            await db.refresh(s)

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
