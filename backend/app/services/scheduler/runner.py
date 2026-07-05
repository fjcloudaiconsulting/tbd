from __future__ import annotations

import datetime

import structlog
from sqlalchemy import select

from app.database import async_session
from app.models.user import Organization
from app.services.scheduler import org_settings
from app.services.scheduler.audit import record_run
from app.services.scheduler.jobs.billing_close import BillingCloseJob
from app.services.scheduler.jobs.billing_reminder import BillingReminderJob
from app.services.scheduler.jobs.recurring_generation import RecurringGenerationJob

logger = structlog.get_logger(__name__)

REGISTRY = [RecurringGenerationJob(), BillingReminderJob(), BillingCloseJob()]


async def run_all_due(today: datetime.date, *, session_factory=async_session, registry=REGISTRY) -> None:
    async with session_factory() as db:
        orgs = (await db.execute(select(Organization))).scalars().all()
    for org in orgs:
        for job in registry:
            async with session_factory() as db:
                try:
                    if not await org_settings.get_bool(db, org.id, job.setting_key):
                        continue
                    if not await job.is_due(db, org, today):
                        continue
                    result = await job.run(db, org, today)
                    await logger.ainfo("scheduler.job.%s" % result.outcome,
                                       job=job.job_type, org_id=org.id, counts=result.counts)
                except Exception as exc:  # noqa: BLE001 — isolate per-job failures
                    await db.rollback()
                    await record_run(job_type=job.job_type, outcome="failure", org=org,
                                     detail={"error": str(exc)})
                    await logger.aerror("scheduler.job.failure", job=job.job_type,
                                        org_id=org.id, error=str(exc))
