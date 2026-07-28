from __future__ import annotations

import datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import NotificationCategory
from app.models.user import Organization
from app.services import billing_service
from app.services.notification_service import dispatch_notification_to_org_members
from app.services.notification_templates import scheduler_billing_closed
from app.services.scheduler import org_settings
from app.services.scheduler.audit import record_run
from app.services.scheduler.base import JobResult

logger = structlog.get_logger(__name__)

# TBD-241 §2. `close_period` performs exactly ONE close per call, so an org
# whose open period is N cycles behind needs N iterations. The cap bounds a
# single tick; the next tick continues where this one stopped, because
# `is_due` stays true.
MAX_CONVERGENCE_STEPS = 24


class BillingCloseJob:
    job_type = "billing_close"
    setting_key = org_settings.AUTOMATE_BILLING_KEY

    async def is_due(self, db: AsyncSession, org: Organization, today: datetime.date) -> bool:
        boundary = billing_service.current_cycle_window(org.billing_cycle_day, today)[0]
        current = await billing_service.get_current_period(db, org.id)
        return current.start_date < boundary

    async def run(self, db: AsyncSession, org: Organization, today: datetime.date) -> JobResult:
        """Converge the org's roster up to the current cycle boundary.

        Loops rather than closing once per tick (TBD-241 §2). `run` produces one
        notification per close event to every org member, so one-step-per-tick
        would turn a three-cycle catch-up into three notifications, three audit
        rows and three tick-budget slots spread over 45 minutes. Convergence
        therefore emits **one** audit row and **one** notification for the whole
        run.

        **Every iteration re-passes the same `boundary - 1`**, recomputed once
        from `today`. Do not re-derive the target from each new open start: the
        clamp inside `close_period` is what advances the roster one cycle at a
        time, and `is_due`'s `current.start_date < boundary` guarantees
        `requested >= current.start_date` on every iteration, so the service's
        lower-bound check can never fire mid-convergence.

        Failure semantics are D11, and the ruling is "do nothing special": a
        mid-convergence exception propagates untouched. `runner.py:67-72`
        already catches every job exception, rolls back and writes a
        `scheduler.billing_close.failure` row; the steps that committed stay
        durable (`close_period` commits internally, so this is N independent
        transactions), `is_due` is still true, and the next tick resumes and
        notifies 900 seconds later. Emitting a partial notification here cannot
        work: the exception D11 exists for leaves the `AsyncSession`
        deactivated, and `dispatch_notification_to_org_members` opens with
        `db.execute`.

        Two consequences are deliberate rather than hidden: a partially
        successful convergence consumes no `max_orgs` budget (because `run`
        raises rather than returning), while on the success path one org can now
        consume up to `MAX_CONVERGENCE_STEPS` closes against a single budget
        slot. And the self-heal claim covers *mid*-convergence failures only —
        if every step commits and the failure lands afterwards (at `record_run`,
        the dispatch, or the final commit) `is_due` is already false and the
        notification is lost until the next cycle boundary. Pre-existing
        single-step behaviour, widened here from one close to up to 24.
        """
        boundary = billing_service.current_cycle_window(org.billing_cycle_day, today)[0]
        close_date = boundary - datetime.timedelta(days=1)

        steps = 0
        iterations = 0
        closed_period_ids: list[int] = []
        closed_on: datetime.date | None = None
        new_period = None

        while True:
            # `close_period`'s return type is frozen (D10), so the ids of the
            # rows this run closes come back through its `closed_ids`
            # out-parameter. `is_due` fetches the closing row only to discard
            # it, so the loop still reads it itself for the progress guard.
            closing = await billing_service.get_current_period(db, org.id)
            # SNAPSHOT both attributes as plain values immediately (code review
            # F2). `close_period`'s D4 path calls `db.rollback()`, which expires
            # the whole identity map and then repopulates only the row at
            # `current_id`; when `close_period`'s own `get_current_period`
            # picked a different row than this one, `closing` stays expired and
            # any attribute read below triggers a SYNC lazy-load inside an async
            # session — `MissingGreenlet`. `routers/settings.py:504-518` applies
            # the same pattern for the same reason.
            closing_id = closing.id
            closing_start = closing.start_date

            applied_before = len(closed_period_ids)
            new_period = await billing_service.close_period(
                db, org.id, close_date, today=today, closed_ids=closed_period_ids,
            )
            iterations += 1

            # `close_period` returns a perfectly good open row on two paths that
            # WRITE NOTHING: the F1 lock (and D4 step 4) finding that a racer
            # already closed this period. D10 defines `closed_period_ids` as the
            # rows whose `end_date` THIS RUN wrote, and `closed_on` as the last
            # applied close date, so neither may be derived from a close we did
            # not perform (code review F3).
            if len(closed_period_ids) > applied_before:
                steps += 1
                # Same derivation the route uses (`settings.py:568`): the
                # resolved close date read back off the row the service opened.
                closed_on = new_period.start_date - datetime.timedelta(days=1)

            if new_period.start_date <= closing_start:
                # Believed unreachable — the clamp always lands strictly after
                # the closing start. A cheap backstop against an infinite loop,
                # and it logs so an unreachable branch that fires is visible.
                await logger.awarning(
                    "billing.close.no_progress",
                    org_id=org.id,
                    closing_period_id=closing_id,
                    closing_period_start=closing_start.isoformat(),
                    new_period_start=new_period.start_date.isoformat(),
                    steps=steps,
                )
                break

            if not await self.is_due(db, org, today):
                break

            # The cap counts ITERATIONS, not applied closes: a run whose every
            # step is absorbed by a racer applies nothing, and a cap keyed on
            # `steps` would then never fire.
            if iterations >= MAX_CONVERGENCE_STEPS:
                await logger.awarning(
                    "billing.close.convergence_capped",
                    org_id=org.id,
                    steps=steps,
                    iterations=iterations,
                    target_close_date=close_date.isoformat(),
                    new_period_start=new_period.start_date.isoformat(),
                )
                break

        # Outside the loop (D11). A no-op for the period writes — `close_period`
        # already committed each step — but it must not run per iteration.
        await db.commit()

        counts = {
            # None only when every iteration was absorbed by a racer, i.e. this
            # run advanced the roster by observation rather than by writing.
            "closed_on": closed_on.isoformat() if closed_on is not None else None,
            "new_period_start": new_period.start_date.isoformat(),
            "steps": steps,
            "closed_period_ids": closed_period_ids,
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
