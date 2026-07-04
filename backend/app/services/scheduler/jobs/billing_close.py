from __future__ import annotations

import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import NotificationCategory
from app.models.user import Organization
from app.services import billing_service
from app.services.notification_service import dispatch_notification_to_org_members
from app.services.notification_templates import scheduler_billing_closed
from app.services.scheduler import org_settings
from app.services.scheduler.audit import record_run
from app.services.scheduler.base import JobResult


class BillingCloseJob:
    job_type = "billing_close"
    setting_key = org_settings.AUTOMATE_BILLING_KEY

    async def is_due(self, db: AsyncSession, org: Organization, today: datetime.date) -> bool:
        boundary = billing_service._snap_to_cycle(today, org.billing_cycle_day)
        current = await billing_service.get_current_period(db, org.id)
        return current.start_date < boundary

    async def run(self, db: AsyncSession, org: Organization, today: datetime.date) -> JobResult:
        boundary = billing_service._snap_to_cycle(today, org.billing_cycle_day)
        close_date = boundary - datetime.timedelta(days=1)
        new_period = await billing_service.close_period(db, org.id, close_date)
        await db.commit()
        counts = {
            "closed_on": close_date.isoformat(),
            "new_period_start": new_period.start_date.isoformat(),
        }
        audit_id = await record_run(job_type=self.job_type, outcome="success", org=org, detail=counts)
        title, body, link = scheduler_billing_closed(new_period_start=new_period.start_date)
        await dispatch_notification_to_org_members(
            db, org_id=org.id, category=NotificationCategory.ORG_ACTIVITY,
            event_type=f"scheduler.{self.job_type}.success",
            title=title, body=body, link_url=link, audit_event_id=audit_id,
        )
        await db.commit()
        return JobResult.ok(counts)
