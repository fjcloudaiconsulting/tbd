"""Audit-event recording and querying (L4.7).

The recording path uses an **independent session** opened from the
engine-wide ``async_sessionmaker`` so the audit write is a separate
transaction from whatever business operation triggered it. Two
properties this gives us:

- A failed business txn (e.g. ``admin.org.delete.failed``) still
  produces an audit row, because the audit write doesn't ride on
  the rolled-back session.
- A failed audit write (DB transient, FK violation, anything) never
  surfaces back to the caller. We log the failure via structlog and
  swallow — the structlog event the caller already emitted is the
  fallback channel.

Caller responsibilities:

- Pass the ``async_sessionmaker`` (not a session). Inject via
  ``Depends(get_session_factory)`` in routers.
- Call **after** ``await db.commit()`` (or after the rollback path)
  so the snapshot fields reflect the state the audit row should
  attest to.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.audit_event import AuditEvent, AuditOutcome


logger = structlog.stdlib.get_logger()


async def record_audit_event(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    event_type: str,
    actor_user_id: Optional[int],
    actor_email: str,
    target_org_id: Optional[int],
    target_org_name: Optional[str],
    request_id: Optional[str],
    ip_address: Optional[str],
    outcome: Literal["success", "failure"],
    detail: Optional[dict[str, Any]] = None,
) -> None:
    """Persist an audit event in its own transaction.

    Failures are logged via structlog and swallowed. Never raises.
    """
    try:
        async with session_factory() as session:
            row = AuditEvent(
                event_type=event_type,
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                target_org_id=target_org_id,
                target_org_name=target_org_name,
                request_id=request_id,
                ip_address=ip_address,
                outcome=AuditOutcome(outcome),
                detail=detail,
            )
            session.add(row)
            await session.commit()
    except Exception as exc:  # noqa: BLE001 — defensive: never bubble.
        await logger.aerror(
            "audit.record.failed",
            event_type=event_type,
            actor_user_id=actor_user_id,
            target_org_id=target_org_id,
            outcome=outcome,
            error=str(exc),
            error_type=type(exc).__name__,
        )


async def list_audit_events(
    db: AsyncSession,
    *,
    actor_user_id: Optional[int] = None,
    target_org_id: Optional[int] = None,
    event_type: Optional[str] = None,
    outcome: Optional[str] = None,
    from_dt: Optional[datetime] = None,
    to_dt: Optional[datetime] = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[AuditEvent], int]:
    """Return ``(rows, total)`` for the admin audit table.

    Ordered by ``created_at DESC`` (then id DESC for stable sort
    across same-timestamp events).
    """
    where = []
    if actor_user_id is not None:
        where.append(AuditEvent.actor_user_id == actor_user_id)
    if target_org_id is not None:
        where.append(AuditEvent.target_org_id == target_org_id)
    if event_type:
        where.append(AuditEvent.event_type == event_type)
    if outcome:
        # Validate against enum so a typo can't silently match nothing.
        try:
            outcome_enum = AuditOutcome(outcome)
        except ValueError:
            outcome_enum = None
        if outcome_enum is not None:
            where.append(AuditEvent.outcome == outcome_enum)
    if from_dt is not None:
        where.append(AuditEvent.created_at >= from_dt)
    if to_dt is not None:
        where.append(AuditEvent.created_at <= to_dt)

    base = select(AuditEvent)
    count_q = select(func.count()).select_from(AuditEvent)
    for clause in where:
        base = base.where(clause)
        count_q = count_q.where(clause)

    total = (await db.execute(count_q)).scalar_one()

    rows_result = await db.execute(
        base.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = list(rows_result.scalars().all())
    return rows, total
