"""Retention purge of agent pending-action rows (TBD-640).

Runs once per tick from ``run_one_tick`` under the tick lock. Every row whose
``created_at`` is older than ``RETENTION_HOURS`` goes, whatever its status:
the row is the whole record of a proposal, and the privacy policy promises it
is kept no longer than that. Errors are logged, never raised (the ticker must
not die).
"""
from __future__ import annotations

import datetime

import logging
from opentelemetry import trace
from sqlalchemy import delete

from app import tracing
from app.database import async_session
from app.models.agent_pending_action import AgentPendingAction

logger = logging.getLogger(__name__)

RETENTION_HOURS = 48


async def run_agent_action_purge(
    session_factory=async_session, *, now: datetime.datetime | None = None
) -> int:
    """Delete rows created more than ``RETENTION_HOURS`` ago. Returns rows deleted."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is not None:  # columns are naive UTC
        now = now.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    try:
        async with session_factory() as db:
            result = await db.execute(delete(AgentPendingAction).where(
                AgentPendingAction.created_at < now - datetime.timedelta(hours=RETENTION_HOURS)
            ))
            await db.commit()
    except Exception as exc:  # noqa: BLE001 -- never let the ticker die
        # Class only: str(exc) quotes the statement and its bound values.
        tracing.mark_error(trace.get_current_span(), exc)
        logger.error("scheduler.agent_action_purge.failed", extra={"error": type(exc).__name__})
        return 0
    if result.rowcount:
        logger.info("scheduler.agent_action_purge.complete", extra={"rows": result.rowcount})
    return result.rowcount
