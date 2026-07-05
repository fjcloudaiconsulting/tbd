from __future__ import annotations

import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.recurring import RecurringTransaction
from app.models.user import Organization
from app.services.billing_service import current_cycle_window
from app.services.notification_service import dispatch_notification_to_org_members
from app.services.notification_templates import scheduler_recurring_generated
from app.services.recurring_service import generate_due_transactions
from app.services.scheduler import org_settings
from app.services.scheduler.audit import record_run
from app.services.scheduler.base import JobResult
from app.models.notification import NotificationCategory


class RecurringGenerationJob:
    job_type = "recurring_generation"
    setting_key = org_settings.AUTOMATE_RECURRING_KEY

    async def is_due(self, db: AsyncSession, org: Organization, today: datetime.date) -> bool:
        _, period_end = current_cycle_window(org.billing_cycle_day, today)
        found = (
            await db.execute(
                select(RecurringTransaction.id).where(
                    RecurringTransaction.org_id == org.id,
                    RecurringTransaction.is_active == True,  # noqa: E712
                    RecurringTransaction.next_due_date <= period_end,
                ).limit(1)
            )
        ).first()
        return found is not None

    async def run(self, db: AsyncSession, org: Organization, today: datetime.date) -> JobResult:
        result = await generate_due_transactions(db, org.id)
        await db.commit()
        generated = int(result.get("generated", 0))
        settled = int(result.get("settled", 0))
        counts = {"generated": generated, "settled": settled, "pending": int(result.get("pending", 0))}
        if generated == 0 and settled == 0:
            return JobResult.noop()
        audit_id = await record_run(job_type=self.job_type, outcome="success", org=org, detail=counts)
        title, body, link = scheduler_recurring_generated(generated=generated, settled=settled)
        await dispatch_notification_to_org_members(
            db, org_id=org.id, category=NotificationCategory.ORG_ACTIVITY,
            event_type=f"scheduler.{self.job_type}.success",
            title=title, body=body, link_url=link, audit_event_id=audit_id,
        )
        await db.commit()
        return JobResult.ok(counts)
