"""Usage-meter admission (TBD-585).

``admit`` counts one use of a meter against the org's resolved limit
(``feature_service.get_entitlements``) or raises :class:`PlanLimitReached`.
Counters live in ``usage_counters`` keyed by (org, meter, period kind, period
start) on the app clock (UTC), and are never refunded: an admitted call that
later fails still counted.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app._time import utcnow_naive
from app.auth.feature_catalog import ALL_METER_KEYS
from app.models.usage_counter import UsageCounter
from app.services import feature_service


class PlanLimitReached(Exception):
    """The org used up ``meter`` for this period. Mapped to HTTP 402 in
    ``app.main`` and to ``ToolError("plan_limit_reached")`` in the registry."""

    def __init__(self, meter: str, limit: int | None, period: str, resets_at: datetime) -> None:
        super().__init__(f"plan limit reached: {meter}")
        self.meter = meter
        self.limit = limit
        self.period = period
        self.resets_at = resets_at


def period_start(period: str, now: datetime) -> date:
    """Start of the UTC day or month holding naive-UTC ``now``."""
    return now.date() if period == "day" else now.date().replace(day=1)


def resets_at(period: str, now: datetime) -> datetime:
    """The next period boundary after ``now``, as an aware UTC datetime."""
    start = period_start(period, now)
    if period == "day":
        nxt = start + timedelta(days=1)
    else:
        nxt = (start + timedelta(days=32)).replace(day=1)
    return datetime.combine(nxt, time(), tzinfo=timezone.utc)


async def _try_increment(
    session: AsyncSession, org_id: int, meter: str, period: str, start: date, n: int,
    limit: int | None,
) -> bool:
    """Add ``n`` to the counter unless that would pass ``limit``; True if added.

    Upsert-then-conditional-UPDATE, each committed, so the check and the
    increment are one atomic statement on a row that already exists (a
    SELECT-then-increment lets two callers both pass at ``limit - 1``).

    ponytail: no retry. An InnoDB deadlock / lock-wait (1213/1205) on the
    FIRST upsert of a new (org, meter, period) row propagates and fails closed
    as ``internal_error``; a retry needs a rollback, which expires the
    caller's ``user`` (MissingGreenlet). Add one, with a user re-load, only if
    it is ever seen.
    """
    row = dict(org_id=org_id, meter=meter, period=period, period_start=start, value=0)
    if session.get_bind().dialect.name == "mysql":
        ins = mysql_insert(UsageCounter).values(**row)
        stmt = ins.on_duplicate_key_update(value=UsageCounter.value)
    else:
        stmt = sqlite_insert(UsageCounter).values(**row).on_conflict_do_nothing()
    await session.execute(stmt)
    await session.commit()

    upd = (
        update(UsageCounter)
        .where(
            UsageCounter.org_id == org_id,
            UsageCounter.meter == meter,
            UsageCounter.period == period,
            UsageCounter.period_start == start,
        )
        .values(value=UsageCounter.value + n)
        .execution_options(synchronize_session=False)
    )
    if limit is not None:
        upd = upd.where(UsageCounter.value + n <= limit)
    result = await session.execute(upd)
    await session.commit()
    return result.rowcount == 1


async def admit(
    db: AsyncSession, org_id: int, meter: str, n: int = 1, *, now: datetime | None = None
) -> None:
    """Count ``n`` uses of ``meter`` for ``org_id`` or raise :class:`PlanLimitReached`.

    Runs in the CALLER's session and COMMITS it (twice), so call it before any
    write of the unit of work. It refuses (``RuntimeError``, fail closed) when
    the session holds pending ORM changes, which the commit would otherwise
    persist. The guard cannot see writes already sent with ``execute()`` and
    not yet committed: a caller that makes one must commit first.
    """
    if db.new or db.dirty or db.deleted:
        raise RuntimeError("usage_service.admit: the session holds uncommitted ORM changes")
    if meter not in ALL_METER_KEYS:
        raise ValueError(f"unknown meter {meter!r}")
    now = now or utcnow_naive()
    ent = await feature_service.get_entitlements(db, org_id, now=now)
    lim = ent.limits[meter]
    if not await _try_increment(
        db, org_id, meter, lim.period, period_start(lim.period, now), n, lim.limit
    ):
        raise PlanLimitReached(meter, lim.limit, lim.period, resets_at(lim.period, now))
