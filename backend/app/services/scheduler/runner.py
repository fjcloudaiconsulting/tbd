from __future__ import annotations

import datetime

import structlog
from sqlalchemy import select

from app.database import async_session
from app.models.user import Organization
from app.services.scheduler import org_settings
from app.services.scheduler.audit import record_run
from app.services.scheduler.base import OUTCOME_SUCCESS
from app.services.scheduler.jobs.billing_close import BillingCloseJob
from app.services.scheduler.jobs.billing_reminder import BillingReminderJob
from app.services.scheduler.jobs.cc_statement_reminder import CcStatementReminderJob
from app.services.scheduler.jobs.recurring_generation import RecurringGenerationJob

logger = structlog.get_logger(__name__)

REGISTRY = [
    RecurringGenerationJob(),
    BillingReminderJob(),
    BillingCloseJob(),
    CcStatementReminderJob(),
]


async def run_all_due(
    today: datetime.date,
    *,
    session_factory=async_session,
    registry=REGISTRY,
    max_orgs: int | None = None,
) -> None:
    """Run every due job for every org.

    ``max_orgs`` caps how many orgs may perform *real work* (a job that returns a
    ``success`` outcome) in a single tick — the rollout guard. On a fresh deploy or
    after long downtime, a large backlog would otherwise close billing periods and
    email every org's members in one burst. Orgs are drained in a stable id order,
    so the orgs skipped this tick are picked up on the next one (durable ``is_due``
    keeps them due). Only orgs that actually do work count against the budget, so a
    steady state with few mutations never starves orgs past the cap. ``None`` or any
    value ``<= 0`` = no cap (pre-guard behavior).
    """
    async with session_factory() as db:
        orgs = (
            await db.execute(select(Organization).order_by(Organization.id))
        ).scalars().all()
    worked_orgs = 0
    for org in orgs:
        org_did_work = False
        for job in registry:
            async with session_factory() as db:
                try:
                    if not await org_settings.get_bool(db, org.id, job.setting_key):
                        continue
                    if not await job.is_due(db, org, today):
                        continue
                    result = await job.run(db, org, today)
                    if result.outcome == OUTCOME_SUCCESS:
                        org_did_work = True
                    await logger.ainfo("scheduler.job.%s" % result.outcome,
                                       job=job.job_type, org_id=org.id, counts=result.counts)
                except Exception as exc:  # noqa: BLE001 — isolate per-job failures
                    await db.rollback()
                    await record_run(job_type=job.job_type, outcome="failure", org=org,
                                     detail={"error": str(exc)})
                    await logger.aerror("scheduler.job.failure", job=job.job_type,
                                        org_id=org.id, error=str(exc))
        if org_did_work:
            worked_orgs += 1
            if max_orgs is not None and max_orgs > 0 and worked_orgs >= max_orgs:
                await logger.ainfo("scheduler.tick.budget_exhausted",
                                   worked_orgs=worked_orgs, max_orgs=max_orgs,
                                   total_orgs=len(orgs))
                break
