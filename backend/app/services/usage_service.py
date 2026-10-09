"""Usage-meter admission (TBD-585).

``admit`` counts one use of a meter against the org's resolved limit
(``feature_service.get_entitlements``) or raises :class:`PlanLimitReached`.
Counters live in ``usage_counters`` keyed by (org, meter, period kind, period
start) on the app clock (UTC), and are never refunded: an admitted call that
later fails still counted.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select, update
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

    def __init__(
        self, meter: str, limit: int | None, period: str, resets_at: datetime | None
    ) -> None:
        super().__init__(f"plan limit reached: {meter}")
        self.meter = meter
        self.limit = limit
        self.period = period
        self.resets_at = resets_at  # None: a 0 limit never resets


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
        # A 0 limit never resets (only a plan or override change reopens it).
        raise PlanLimitReached(
            meter, lim.limit, lim.period, None if lim.limit == 0 else resets_at(lim.period, now)
        )



async def current_usage(
    db: AsyncSession, org_id: int, *, include_platform: bool, now: datetime | None = None
) -> dict[str, dict]:
    """This period's use of each meter (TBD-581), keyed by meter: ``used``,
    ``limit`` (None = unlimited, 0 = closed), ``period`` and ``resets_at``
    (None for a 0 limit, which never resets). ``platform_ai.*`` is the org's
    platform spend: only for admins (``include_platform``), and left out at a 0
    limit so dark platform AI stays dark."""
    now = now or utcnow_naive()
    ent = await feature_service.get_entitlements(db, org_id, now=now)
    lims = {
        m: ent.limits[m] for m in sorted(ALL_METER_KEYS)
        if not m.startswith("platform_ai.") or (include_platform and ent.limits[m].limit != 0)
    }
    rows = (await db.execute(
        select(UsageCounter.meter, UsageCounter.period, UsageCounter.period_start, UsageCounter.value)
        .where(UsageCounter.org_id == org_id, UsageCounter.meter.in_(lims))
    )).all()
    used = {
        r.meter: r.value for r in rows
        if (r.period, r.period_start) == (lims[r.meter].period, period_start(lims[r.meter].period, now))
    }
    return {
        m: {
            "used": used.get(m, 0), "limit": lim.limit, "period": lim.period,
            "resets_at": None if lim.limit == 0 else resets_at(lim.period, now),
        }
        for m, lim in lims.items()
    }
