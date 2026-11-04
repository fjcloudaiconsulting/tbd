"""Retention purge of batch-create idempotency keys (TBD-784).

Runs once per tick from ``run_one_tick`` under the tick lock. A key replays
for ``KEY_TTL_HOURS`` only; after that its row is dead weight and goes.
Errors are logged, never raised (the ticker must not die).
"""
from __future__ import annotations

import datetime

import logging
from opentelemetry import trace
from sqlalchemy import delete

from app import tracing
from app._time import utcnow_naive
from app.database import async_session
from app.models.transaction_batch_key import TransactionBatchKey
from app.services.transaction_batch_service import KEY_TTL_HOURS

logger = logging.getLogger(__name__)


async def run_batch_key_purge(
    session_factory=async_session, *, now: datetime.datetime | None = None
) -> int:
    """Delete keys created more than ``KEY_TTL_HOURS`` ago. Returns rows deleted."""
    now = now or utcnow_naive()
    try:
        async with session_factory() as db:
            result = await db.execute(delete(TransactionBatchKey).where(
                TransactionBatchKey.created_at < now - datetime.timedelta(hours=KEY_TTL_HOURS)
            ))
            await db.commit()
    except Exception as exc:  # noqa: BLE001 -- never let the ticker die
        # Class only: str(exc) quotes the statement and its bound values.
        tracing.mark_error(trace.get_current_span(), exc)
        logger.error("scheduler.batch_key_purge.failed", extra={"error": type(exc).__name__})
        return 0
    if result.rowcount:
        logger.info("scheduler.batch_key_purge.complete", extra={"rows": result.rowcount})
    return result.rowcount
