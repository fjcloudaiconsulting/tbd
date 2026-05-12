"""System-usage analytics aggregations (L4.6 — counts-only slice).

Pure SQL aggregates over existing tables. No third-party analytics
SDK; no per-user/per-event PII; no real-time streams. The shape is
deliberately small (six functions) so each can be evolved into a
charts source later without breaking the envelope.

Data sources:

- ``logins_by_day``  → ``audit_events`` filtered by
  ``event_type = 'user.login.success'``. The login endpoint emits
  this event on every successful sign-in (see PR L4.6).
- ``tx_writes_by_day`` / ``imports_by_day`` / ``top_orgs_by_tx_volume``
  / ``dormant_orgs`` → ``transactions`` table, grouped by
  ``DATE(created_at)`` (creation time, not transaction_date — the
  spec wants write activity).
- ``imports_by_day`` → counts ``transactions`` rows with
  ``is_imported = TRUE``. The product does not persist an
  ``import_sessions`` table, so this is "imported rows per day",
  not "import sessions per day". A single CSV import may contribute
  dozens of rows; document this when surfacing.

Cross-org isolation:

- ``logins_by_day`` is platform-global by design (it's a system-usage
  signal). The route is platform-only (``analytics.view`` permission)
  so there's no tenant context to scope against.
- ``tx_writes_by_day`` and ``imports_by_day`` are platform-global
  (sum across all orgs) — again, the system view.
- ``top_orgs_by_tx_volume`` and ``dormant_orgs`` are cross-org by
  necessity; rank is over all orgs.

Performance note:

- All queries are single-pass aggregates over indexed columns
  (``transactions.created_at`` is indexed via the org_id+created_at
  pattern used throughout the app; ``audit_events.created_at`` is
  indexed by 030). For dev/PR-1 scale this is sub-millisecond. Watch
  cardinality if the dataset crosses ~10M rows — at that point cache
  the response or pre-aggregate into a daily-rollup table.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_event import AuditEvent
from app.models.transaction import Transaction
from app.models.user import Organization


LOGIN_AUDIT_EVENT_TYPE = "user.login.success"


def _normalize_days(days: int) -> int:
    """Clamp the window to a sane range. The route surfaces the value
    so an obvious upper bound matters for latency budgeting more than
    for correctness."""
    if days <= 0:
        return 30
    if days > 365:
        return 365
    return days


def _window_start(days: int, *, now: datetime | None = None) -> datetime:
    """UTC midnight ``days`` days ago. We pin to midnight so the same
    request issued twice on the same day returns the same buckets."""
    base = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    midnight = base.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=days - 1)


def _coerce_to_date(value: Any) -> date | None:
    """Normalize ``func.date()`` output across drivers.

    MySQL returns ``date`` objects. SQLite returns ISO date strings.
    Some configurations return ``datetime``. Anything else is treated
    as malformed and dropped (caller's responsibility to skip None).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        # ISO date string (SQLite default for DATE()).
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _fill_daily_series(
    rows: list[tuple[Any, int]],
    *,
    window_days: int,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Convert a ``(date_value, count)`` rowset into a contiguous series
    with zero-fill for missing days. The frontend assumes ``window_days``
    points; emitting fewer would force per-render gap-filling there."""
    counts: dict[date, int] = {}
    for raw_d, c in rows:
        d = _coerce_to_date(raw_d)
        if d is None:
            continue
        counts[d] = int(c or 0)

    base = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    today = base.date()
    series: list[dict[str, Any]] = []
    for i in range(window_days):
        the_day = today - timedelta(days=window_days - 1 - i)
        series.append({"date": the_day, "count": counts.get(the_day, 0)})
    return series


async def login_counts_by_day(
    db: AsyncSession,
    days: int = 30,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Daily count of successful logins across the platform.

    Source: ``audit_events`` rows with
    ``event_type = 'user.login.success'``. Days with zero logins are
    emitted with ``count = 0`` so the series is always ``days`` long.
    """
    window_days = _normalize_days(days)
    start = _window_start(window_days, now=now)
    # ``func.date()`` is supported by both MySQL and SQLite and yields
    # a date/iso-string the row mapper can ingest without a typed cast.
    day_col = func.date(AuditEvent.created_at).label("day")
    stmt = (
        select(day_col, func.count().label("c"))
        .where(AuditEvent.event_type == LOGIN_AUDIT_EVENT_TYPE)
        .where(AuditEvent.created_at >= start)
        .group_by(day_col)
        .order_by(day_col)
    )
    rows = (await db.execute(stmt)).all()
    return _fill_daily_series(
        [(r.day, r.c) for r in rows],
        window_days=window_days,
        now=now,
    )


async def tx_writes_by_day(
    db: AsyncSession,
    days: int = 30,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Daily count of transactions created across the platform.

    Uses ``transactions.created_at`` (write time), NOT
    ``transaction_date`` (purchase / accounting date) — the spec
    wants write activity for the system-usage view.
    """
    window_days = _normalize_days(days)
    start = _window_start(window_days, now=now)
    day_col = func.date(Transaction.created_at).label("day")
    stmt = (
        select(day_col, func.count().label("c"))
        .where(Transaction.created_at >= start)
        .group_by(day_col)
        .order_by(day_col)
    )
    rows = (await db.execute(stmt)).all()
    return _fill_daily_series(
        [(r.day, r.c) for r in rows],
        window_days=window_days,
        now=now,
    )


async def imports_by_day(
    db: AsyncSession,
    days: int = 30,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Daily count of imported transactions across the platform.

    We do not persist an ``import_sessions`` table, so this returns
    "imported rows per day" rather than "import sessions per day".
    Surfaces should label accordingly. Filter: ``is_imported = TRUE``.
    """
    window_days = _normalize_days(days)
    start = _window_start(window_days, now=now)
    day_col = func.date(Transaction.created_at).label("day")
    stmt = (
        select(day_col, func.count().label("c"))
        .where(Transaction.created_at >= start)
        .where(Transaction.is_imported.is_(True))
        .group_by(day_col)
        .order_by(day_col)
    )
    rows = (await db.execute(stmt)).all()
    return _fill_daily_series(
        [(r.day, r.c) for r in rows],
        window_days=window_days,
        now=now,
    )


async def top_orgs_by_tx_volume(
    db: AsyncSession,
    limit: int = 10,
    days: int = 30,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Top N orgs by transactions created in the window. Newest orgs
    surface here too — the join key is ``transactions.org_id``, so an
    org with zero transactions is not represented (intentional; that's
    what ``dormant_orgs`` is for).

    Tie-break: secondary sort by ``org_id`` ascending so the result is
    deterministic when two orgs have identical counts.
    """
    window_days = _normalize_days(days)
    if limit <= 0:
        return []
    if limit > 100:
        limit = 100
    start = _window_start(window_days, now=now)
    stmt = (
        select(
            Organization.id.label("org_id"),
            Organization.name.label("org_name"),
            func.count(Transaction.id).label("tx_count"),
        )
        .join(Transaction, Transaction.org_id == Organization.id)
        .where(Transaction.created_at >= start)
        .group_by(Organization.id, Organization.name)
        .order_by(func.count(Transaction.id).desc(), Organization.id.asc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    return [
        {
            "rank": i + 1,
            "org_id": r.org_id,
            "org_name": r.org_name,
            "tx_count": int(r.tx_count or 0),
        }
        for i, r in enumerate(rows)
    ]


async def dormant_orgs(
    db: AsyncSession,
    threshold_days: int = 30,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Orgs whose most recent transaction is older than the threshold
    (or that have never recorded a transaction at all).

    Sorted by oldest-last-activity first so the most-stale orgs lead.
    Orgs with ``last_tx_at = NULL`` (no transactions ever) appear at
    the top — ``ORDER BY last_tx_at NULLS FIRST`` semantics emulated
    via a sentinel column (MySQL has no NULLS FIRST, so we compute a
    boolean ``has_any_tx`` and order by it).
    """
    if threshold_days < 0:
        threshold_days = 0
    if limit <= 0:
        return []
    if limit > 500:
        limit = 500
    base = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = base - timedelta(days=threshold_days)

    last_tx_subq = (
        select(
            Transaction.org_id.label("org_id"),
            func.max(Transaction.created_at).label("last_tx_at"),
        )
        .group_by(Transaction.org_id)
        .subquery()
    )

    has_any_tx = last_tx_subq.c.last_tx_at.isnot(None)
    stmt = (
        select(
            Organization.id.label("org_id"),
            Organization.name.label("org_name"),
            last_tx_subq.c.last_tx_at,
        )
        .join(
            last_tx_subq,
            last_tx_subq.c.org_id == Organization.id,
            isouter=True,
        )
        .where(
            # Either no transactions at all, or last write before the
            # cutoff. Both indicate dormancy.
            (last_tx_subq.c.last_tx_at.is_(None))
            | (last_tx_subq.c.last_tx_at < cutoff)
        )
        # Stable order: never-active orgs first (has_any_tx = False),
        # then oldest-last-activity. Ties broken by org_id ASC.
        .order_by(
            has_any_tx.asc(),
            last_tx_subq.c.last_tx_at.asc(),
            Organization.id.asc(),
        )
        .limit(limit)
    )

    rows = (await db.execute(stmt)).all()
    out: list[dict[str, Any]] = []
    for r in rows:
        last_at = r.last_tx_at
        if last_at is None:
            days_since: int | None = None
        else:
            # Some MySQL drivers return naive datetimes for TIMESTAMP/
            # DATETIME columns; normalize to UTC-aware before subtraction.
            if last_at.tzinfo is None:
                last_at_aware = last_at.replace(tzinfo=timezone.utc)
            else:
                last_at_aware = last_at.astimezone(timezone.utc)
            delta = base - last_at_aware
            days_since = max(0, delta.days)
        out.append(
            {
                "org_id": r.org_id,
                "org_name": r.org_name,
                "last_tx_at": last_at,
                "days_since_last_activity": days_since,
            }
        )
    return out


async def build_analytics_payload(
    db: AsyncSession,
    *,
    days: int = 30,
    top_orgs_limit: int = 10,
    dormant_threshold_days: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Single round-trip composition of the analytics page payload.

    Sequential aggregates rather than ``asyncio.gather`` — SQLAlchemy's
    ``AsyncSession`` is not safe under concurrent task access (same
    rationale as ``admin_dashboard_service.build_dashboard_payload``).
    Each aggregate is sub-millisecond at dev scale; if this ever shows
    up in flame graphs, collapse into a single SELECT with scalar
    subqueries before reintroducing concurrent session use.
    """
    base_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    window_days = _normalize_days(days)

    logins = await login_counts_by_day(db, days=window_days, now=base_now)
    tx_writes = await tx_writes_by_day(db, days=window_days, now=base_now)
    imports = await imports_by_day(db, days=window_days, now=base_now)
    top_orgs = await top_orgs_by_tx_volume(
        db, limit=top_orgs_limit, days=window_days, now=base_now
    )
    dormant = await dormant_orgs(
        db, threshold_days=dormant_threshold_days, now=base_now
    )

    return {
        "window_days": window_days,
        "generated_at": base_now,
        "logins_by_day": logins,
        "tx_writes_by_day": tx_writes,
        "imports_by_day": imports,
        "top_orgs_by_tx_volume": top_orgs,
        "dormant_orgs": dormant,
    }
