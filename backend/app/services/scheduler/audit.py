from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.audit_event import AuditEvent
from app.models.user import Organization
from app.services.audit_service import record_audit_event

REMINDER_EVENT_TYPE = "scheduler.billing_close.reminder"


async def record_run(*, job_type: str, outcome: str, org: Organization, detail: dict[str, Any]) -> int | None:
    return await record_audit_event(
        async_session,
        event_type=f"scheduler.{job_type}.{outcome}",
        actor_user_id=None,
        actor_email="system",
        target_org_id=org.id,
        target_org_name=org.name,
        request_id=None,
        ip_address=None,
        outcome=outcome,  # "success" | "failure"
        detail=detail,
    )


async def record_reminder(*, org: Organization, period_start: datetime.date, detail: dict[str, Any]) -> int | None:
    payload = dict(detail)
    payload["period_start"] = period_start.isoformat()
    return await record_audit_event(
        async_session,
        event_type=REMINDER_EVENT_TYPE,
        actor_user_id=None,
        actor_email="system",
        target_org_id=org.id,
        target_org_name=org.name,
        request_id=None,
        ip_address=None,
        outcome="success",
        detail=payload,
    )


async def reminder_already_sent(db: AsyncSession, org_id: int, period_start: datetime.date) -> bool:
    iso = period_start.isoformat()
    rows = (
        await db.execute(
            select(AuditEvent).where(
                AuditEvent.event_type == REMINDER_EVENT_TYPE,
                AuditEvent.target_org_id == org_id,
            )
        )
    ).scalars().all()
    return any((r.detail or {}).get("period_start") == iso for r in rows)
