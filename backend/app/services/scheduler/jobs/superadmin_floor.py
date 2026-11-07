"""Detect an install with no (or one) active platform superadmin (TBD-443).

Runs once per tick from ``run_one_tick`` under the tick lock. At zero active
superadmins on a populated install ``/admin`` 403s everyone, so the dashboard
cannot show it: this probe is the detector. It logs
``platform.superadmin.none`` (error) at zero and ``platform.superadmin.single``
(warning) when the count changes to one, and publishes two observable
gauges: ``tbd.platform.active_superadmins`` (the count) and
``tbd.platform.active_superadmins.probed_at`` (epoch seconds of the last
successful read). An observable gauge re-exports its last value forever, so a
stalled ticker keeps reporting a fresh-looking count: alert on the AGE of
``probed_at``, not on the count's series going missing. The zero ERROR repeats
every tick; the single WARNING is logged once per change (process-local, so
once more after a restart). An empty install (no users) is a normal first run and stays
quiet. Recovery: ``docs/operations/SUPERADMIN_RECOVERY.md``. Errors are
logged, never raised (the ticker must not die).
"""
from __future__ import annotations

import logging
import time

from opentelemetry import metrics, trace
from sqlalchemy import func, select

from app import tracing
from app.database import async_session
from app.models.user import User

logger = logging.getLogger(__name__)

_gauges: dict[str, float] = {}


def _observe(name: str):
    return lambda _options: [metrics.Observation(_gauges[name])] if name in _gauges else []


_meter = metrics.get_meter(__name__)
_meter.create_observable_gauge("tbd.platform.active_superadmins", [_observe("n")])
_meter.create_observable_gauge(
    "tbd.platform.active_superadmins.probed_at", [_observe("probed_at")], unit="s"
)


async def run_superadmin_floor_probe(session_factory=async_session) -> int | None:
    """Return the active superadmin count, or ``None`` on a failed read."""
    try:
        async with session_factory() as db:
            users = await db.scalar(select(func.count()).select_from(User)) or 0
            active = await db.scalar(
                select(func.count())
                .select_from(User)
                .where(User.is_superadmin.is_(True), User.is_active.is_(True))
            ) or 0
    except Exception as exc:  # noqa: BLE001 -- never let the ticker die
        tracing.mark_error(trace.get_current_span(), exc)
        logger.error("scheduler.superadmin_floor.failed", extra={"error": type(exc).__name__})
        return None
    previous = _gauges.get("n")
    _gauges["n"] = active
    _gauges["probed_at"] = time.time()
    if users and active == 0:
        logger.error("platform.superadmin.none", extra={"active_superadmins": 0, "users": users})
    elif active == 1 and previous != 1:
        logger.warning("platform.superadmin.single", extra={"active_superadmins": 1})
    return active
