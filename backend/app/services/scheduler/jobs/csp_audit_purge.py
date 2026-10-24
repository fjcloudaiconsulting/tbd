"""Retention purge of CSP violation audit rows (BE-10).

Runs once per tick from ``run_one_tick`` under the tick lock. Deletes
``security.csp_violation`` rows created more than ``RETENTION_DAYS`` ago and
nothing else: the anonymous CSP report sink is the table's high-volume writer,
and every other event type is audit history that stays. Rows go in batches of
at most ``BATCH_SIZE``, each in its own short transaction, so a backlog never
holds a long lock; a tick stops after ``MAX_BATCHES`` and the next one carries
on. Logs a row count only. Errors are logged, never raised (the ticker must
not die).
"""
from __future__ import annotations

import datetime

import logging
from opentelemetry import trace
from sqlalchemy import delete, select

from app import tracing
from app.database import async_session
from app.models.audit_event import AuditEvent
from app.services.audit_service import CSP_VIOLATION_EVENT_TYPE

logger = logging.getLogger(__name__)

RETENTION_DAYS = 30
BATCH_SIZE = 1000
# 50,000 rows a tick, far above what the sink can record (its global hourly
# budget plus the per-directive reserve), so the steady state drains each tick.
MAX_BATCHES = 50


async def run_csp_audit_purge(
    session_factory=async_session, *, now: datetime.datetime | None = None
) -> int:
    """Delete CSP violation rows older than ``RETENTION_DAYS``. Returns rows deleted."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is not None:  # columns are naive UTC
        now = now.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    cutoff = now - datetime.timedelta(days=RETENTION_DAYS)
    old_csp_rows = (
        AuditEvent.event_type == CSP_VIOLATION_EVENT_TYPE,
        AuditEvent.created_at < cutoff,
    )
    deleted = 0
    try:
        for _ in range(MAX_BATCHES):
            async with session_factory() as db:
                # The event_type index orders by id within the type, so the
                # oldest rows come first and the scan stops at the limit.
                ids = list((await db.execute(
                    select(AuditEvent.id).where(*old_csp_rows).order_by(AuditEvent.id).limit(BATCH_SIZE)
                )).scalars())
                if not ids:
                    break
                # The predicate again, so an id can only ever name an old CSP row.
                result = await db.execute(
                    delete(AuditEvent).where(AuditEvent.id.in_(ids), *old_csp_rows)
                )
                await db.commit()
            deleted += result.rowcount
            if len(ids) < BATCH_SIZE:
                break
    except Exception as exc:  # noqa: BLE001 -- never let the ticker die
        # Class only: str(exc) quotes the statement and its bound values.
        tracing.mark_error(trace.get_current_span(), exc)
        logger.error(
            "scheduler.csp_audit_purge.failed",
            extra={"error": type(exc).__name__, "rows": deleted},
        )
        return deleted
    if deleted:
        logger.info("scheduler.csp_audit_purge.complete", extra={"rows": deleted})
    return deleted
