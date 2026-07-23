"""Statement-close alert scheduler job for credit-card accounts (Task 9,
CC Statement Alerts V1).

Fires once a credit card's statement cycle has actually CLOSED (as
opposed to Task 8's pre-close reminder). Reports the carried balance --
if any -- as of that close date, using the exact same ledger
reconstruction the forecast bills (``cc_statement_service.statement_outstanding``),
so the alerted amount can never drift from what the forecast shows.

Dual-channel with an asymmetric amount policy (D9/D10): the in-app body
may state the amount due, the email body never does (an amount owed is
sensitive; email is a less-trusted channel), and a $0 outstanding
balance suppresses the email entirely while still fanning out in-app
("nothing due") and writing the dedup marker -- otherwise ``is_due``
would re-evaluate (and re-run the ledger query for) the same closed
cycle on every subsequent tick.

One org can have several cards on different cycles, so both ``is_due``
and ``run`` fan out per-card. ``owed`` is computed and captured into a
frozen, DB-object-free snapshot BEFORE the per-card dispatch loop begins
(same fix as Task 8's ``_DueCard``): a failure on one card triggers
``db.rollback()``, which expires every ORM instance the shared session
has touched -- including OTHER cards' ``Account`` rows fetched by the
same ``active_cc_accounts`` call. Computing ``owed`` (which itself needs
the still-fresh ``Account`` for ``opening_balance``/``currency``) during
snapshot construction, before any card's try/except can roll back, is
what keeps a prior card's failure from poisoning a later card's read.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import NotificationCategory
from app.models.user import Organization
from app.services.cc_statement_service import statement_outstanding
from app.services.notification_service import dispatch_notification_to_org_members
from app.services.notification_templates import scheduler_cc_statement_closed
from app.services.scheduler import org_settings
from app.services.scheduler.audit import (
    CC_CLOSED_EVENT_TYPE,
    cc_alerts_sent_since,
    record_cc_alert,
    record_run,
)
from app.services.scheduler.base import JobResult
from app.services.scheduler.jobs.cc_statement_common import (
    active_cc_accounts,
    most_recent_closed_cycle,
)

# Dedup lookback window: mirrors Task 8's rationale -- wide enough that a
# missed tick can never re-derive a close date this job already alerted
# on, bounded so the query stays cheap.
_DEDUP_LOOKBACK_DAYS = 40


def _format_money(amount: Decimal) -> str:
    """Grouped, 2-decimal amount string (e.g. ``"1,240.00"``).

    No existing backend amount/currency formatter was found to reuse
    (searched for ``format_money``/``format_currency``/``format_amount``);
    this is deliberately currency-agnostic -- the currency code is
    appended separately by the template, never baked in here.
    """
    return f"{amount:,.2f}"


@dataclass(frozen=True)
class _DueCard:
    """Plain-value snapshot of one due card, including its already-computed
    owed amount. See the module docstring for why ``owed`` must be
    resolved before this dataclass is built, not inside the dispatch loop.
    """

    account_id: int
    account_name: str
    currency: str
    close_date: datetime.date
    payment_date: datetime.date
    owed: Decimal


class CcStatementCloseJob:
    job_type = "cc_statement_closed"
    setting_key = org_settings.AUTOMATE_CC_STATEMENT_KEY

    async def _closed_cycles(self, db: AsyncSession, org: Organization, today: datetime.date):
        """Return ``(account, cycle)`` pairs for every card whose most
        recently closed cycle is not yet alerted. Deliberately does NO
        balance math -- that's the expensive part, reserved for ``run``.
        Fetches the sent-set ONCE (not per card).
        """
        accounts = await active_cc_accounts(db, org.id)
        if not accounts:
            return []

        since = today - datetime.timedelta(days=_DEDUP_LOOKBACK_DAYS)
        sent = await cc_alerts_sent_since(db, org.id, CC_CLOSED_EVENT_TYPE, since)

        due = []
        for account in accounts:
            cycle = most_recent_closed_cycle(account, today)
            if cycle is None:
                continue  # backfill guard: no real close to alert on
            key = (account.id, cycle.period_end_inclusive.isoformat())
            if key in sent:
                continue
            due.append((account, cycle))
        return due

    async def is_due(self, db: AsyncSession, org: Organization, today: datetime.date) -> bool:
        return bool(await self._closed_cycles(db, org, today))

    async def _due_cards(self, db: AsyncSession, org: Organization, today: datetime.date) -> list[_DueCard]:
        """Resolve owed-per-card while every ``Account`` row from
        ``_closed_cycles`` is still fresh, and snapshot the result into
        plain data. Called once at the top of ``run``, before the
        dispatch loop that may roll back the shared session.
        """
        closed = await self._closed_cycles(db, org, today)
        due: list[_DueCard] = []
        for account, cycle in closed:
            owed = await statement_outstanding(
                db, org_id=org.id, account=account, close_date=cycle.period_end_inclusive
            )
            due.append(
                _DueCard(
                    account_id=account.id,
                    account_name=account.name,
                    currency=account.currency,
                    close_date=cycle.period_end_inclusive,
                    payment_date=cycle.payment_date,
                    owed=owed,
                )
            )
        return due

    async def run(self, db: AsyncSession, org: Organization, today: datetime.date) -> JobResult:
        due = await self._due_cards(db, org, today)
        dispatched_account_ids: list[int] = []

        for card in due:
            try:
                # Marker FIRST -- same ordering rationale as Task 8: a
                # crash mid-dispatch must never leave the close event
                # un-recorded while still landing in the user's
                # notifications.
                await record_cc_alert(
                    org=org,
                    account_id=card.account_id,
                    close_date=card.close_date,
                    event_type=CC_CLOSED_EVENT_TYPE,
                    detail={},  # no dollar amount in the stored audit detail
                )
                amount_str = _format_money(card.owed) if card.owed > 0 else None
                title, in_app_body, email_body, link = scheduler_cc_statement_closed(
                    card.account_name, amount_str, card.currency, card.payment_date, card.account_id
                )
                send_email = card.owed > 0  # $0 due -> in-app only (D10)
                await dispatch_notification_to_org_members(
                    db,
                    org_id=org.id,
                    category=NotificationCategory.CC_STATEMENT,
                    event_type=CC_CLOSED_EVENT_TYPE,
                    title=title,
                    body=in_app_body,
                    email_body=email_body,
                    link_url=link,
                    send_email=send_email,
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
