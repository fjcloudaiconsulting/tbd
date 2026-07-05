from __future__ import annotations

import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import NotificationCategory
from app.models.user import Organization
from app.services.billing_service import current_cycle_window
from app.services.notification_service import dispatch_notification_to_org_members
from app.services.notification_templates import scheduler_billing_close_reminder
from app.services.scheduler import org_settings
from app.services.scheduler.audit import record_reminder, reminder_already_sent
from app.services.scheduler.base import JobResult
from app.services.scheduler.org_settings import get_reminder_lead_days


class BillingReminderJob:
    job_type = "billing_reminder"
    setting_key = org_settings.AUTOMATE_BILLING_KEY

    def _next_boundary(self, org: Organization, today: datetime.date) -> datetime.date:
        _, period_end = current_cycle_window(org.billing_cycle_day, today)
        return period_end + datetime.timedelta(days=1)

    async def is_due(self, db: AsyncSession, org: Organization, today: datetime.date) -> bool:
        boundary = self._next_boundary(org, today)
        days_until = (boundary - today).days
        lead = await get_reminder_lead_days(db, org.id)
        if not (0 < days_until <= lead):
            return False
        return not await reminder_already_sent(db, org.id, boundary)

    async def run(self, db: AsyncSession, org: Organization, today: datetime.date) -> JobResult:
        boundary = self._next_boundary(org, today)
        days_until = (boundary - today).days
        audit_id = await record_reminder(org=org, period_start=boundary, detail={"days_until": days_until})
        title, body, link = scheduler_billing_close_reminder(close_date=boundary, days_until=days_until)
        await dispatch_notification_to_org_members(
            db, org_id=org.id, category=NotificationCategory.ORG_ACTIVITY,
            event_type="scheduler.billing_close.reminder",
            title=title, body=body, link_url=link, audit_event_id=audit_id,
        )
        await db.commit()
        return JobResult.ok({"period_start": boundary.isoformat(), "days_until": days_until})
