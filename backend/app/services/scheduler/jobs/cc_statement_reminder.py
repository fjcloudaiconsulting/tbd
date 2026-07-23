"""Pre-close reminder scheduler job for credit-card statements (Task 8,
CC Statement Alerts V1).

Fires a few days before a credit card's next statement closes -- in-app
only, since the amount due isn't known yet (that's the close job, Task 9).
One org can have several cards on different cycles, so both ``is_due``
and ``run`` fan out per-card rather than per-org, and a failure on one
card must not poison the others sharing the request's ``db`` session
(see the per-card ``try/except`` + ``rollback`` in ``run``).
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import NotificationCategory
from app.models.user import Organization
from app.services.cc_cycle_service import resolve_cycle_for_account
from app.services.notification_service import dispatch_notification_to_org_members
from app.services.notification_templates import scheduler_cc_statement_reminder
from app.services.scheduler import org_settings
from app.services.scheduler.audit import (
    CC_REMINDER_EVENT_TYPE,
    cc_alerts_sent_since,
    record_cc_alert,
    record_run,
)
from app.services.scheduler.base import JobResult
from app.services.scheduler.jobs.cc_statement_common import active_cc_accounts
from app.services.scheduler.org_settings import get_cc_statement_lead_days

# Dedup lookback window: wide enough that a lead-day change or a missed
# tick can never re-derive a close date this job already alerted on, but
# bounded so the query stays cheap (mirrors billing_reminder's approach
# of anchoring dedup on the derived boundary, not an unbounded scan).
_DEDUP_LOOKBACK_DAYS = 40


@dataclass(frozen=True)
class _DueCard:
    """Plain-value snapshot of one due card.

    Deliberately holds no ORM object. ``run`` iterates a list of these
    across a shared ``db`` session, and a mid-loop ``db.rollback()`` (one
    card's failure) expires every ORM instance that session has ever
    loaded -- including OTHER cards' ``Account`` rows fetched by the same
    ``active_cc_accounts`` call. Touching an expired attribute afterwards
    would trigger an implicit lazy-load that ``AsyncSession`` cannot
    service from plain sync attribute access (``MissingGreenlet``). Every
    value the per-card loop body needs must be captured here, up front,
    before any card's try/except can roll back.
    """

    account_id: int
    account_name: str
    close_date: datetime.date
    days_until: int


class CcStatementReminderJob:
    job_type = "cc_statement_reminder"
    setting_key = org_settings.AUTOMATE_CC_STATEMENT_KEY

    async def _due_cards(
        self, db: AsyncSession, org: Organization, today: datetime.date
    ) -> list[_DueCard]:
        """Return every card that is both within its reminder lead window
        and not yet alerted for this cycle's close date. Fetches the
        sent-set ONCE (not per card).
        """
        accounts = await active_cc_accounts(db, org.id)
        if not accounts:
            return []

        lead = await get_cc_statement_lead_days(db, org.id)
        since = today - datetime.timedelta(days=_DEDUP_LOOKBACK_DAYS)
        sent = await cc_alerts_sent_since(db, org.id, CC_REMINDER_EVENT_TYPE, since)

        due: list[_DueCard] = []
        for account in accounts:
            cycle = resolve_cycle_for_account(account, today)
            days_until = (cycle.period_end_inclusive - today).days
            key = (account.id, cycle.period_end_inclusive.isoformat())
            if 0 < days_until <= lead and key not in sent:
                due.append(
                    _DueCard(
                        account_id=account.id,
                        account_name=account.name,
                        close_date=cycle.period_end_inclusive,
                        days_until=days_until,
                    )
                )
        return due

    async def is_due(self, db: AsyncSession, org: Organization, today: datetime.date) -> bool:
        return bool(await self._due_cards(db, org, today))

    async def run(self, db: AsyncSession, org: Organization, today: datetime.date) -> JobResult:
        due = await self._due_cards(db, org, today)
        dispatched_account_ids: list[int] = []

        for card in due:
            try:
                # Marker FIRST so a crash mid-dispatch never leaves the
                # reminder un-recorded while still landing in the user's
                # notifications (that ordering is the one that could
                # spam, not the one that could double-send silently).
                await record_cc_alert(
                    org=org,
                    account_id=card.account_id,
                    close_date=card.close_date,
                    event_type=CC_REMINDER_EVENT_TYPE,
                    detail={"days_until": card.days_until},
                )
                title, body, link = scheduler_cc_statement_reminder(
                    card.account_name, card.close_date, card.days_until, card.account_id
                )
                await dispatch_notification_to_org_members(
                    db,
                    org_id=org.id,
                    category=NotificationCategory.CC_STATEMENT,
                    event_type=CC_REMINDER_EVENT_TYPE,
                    title=title,
                    body=body,
                    link_url=link,
                    send_email=False,
                )
                await db.commit()
                dispatched_account_ids.append(card.account_id)
            except Exception as exc:  # noqa: BLE001 — isolate per-card failures
                # Shared session: roll back before touching it again, or
                # the next card's commit hits PendingRollbackError.
                await db.rollback()
                await record_run(
                    job_type=self.job_type,
                    outcome="failure",
                    org=org,
                    detail={"account_id": card.account_id, "error": str(exc)},
                )
                continue

        if dispatched_account_ids:
            return JobResult.ok(
                {"dispatched_account_ids": dispatched_account_ids, "count": len(dispatched_account_ids)}
            )
        return JobResult.noop()
