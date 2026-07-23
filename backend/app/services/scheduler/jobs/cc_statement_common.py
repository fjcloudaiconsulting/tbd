"""Shared helpers for the CC-statement scheduler jobs (Tasks 8/9).

Two building blocks used by both the close-alert and payment-due-alert jobs:

- ``active_cc_accounts``: the org-scoped set of credit-card accounts that
  actually carry a cycle (``close_day IS NOT NULL``) and are active. Mirrors
  the account+type JOIN in ``account_balance_forecast_service.py`` (lines
  ~70-76), scoped down to ``account_type.slug == "credit_card"`` and
  ``close_day IS NOT NULL``. Deliberately does NOT require
  ``payment_source_account_id`` — a CC without a payment source still closes
  and still needs a close-alert; the payment-source dependency is a
  Task 8/9 concern, not this query's.

- ``most_recent_closed_cycle``: given an account and "today", return the
  most recently CLOSED ``CreditCardCycle`` — i.e. the cycle whose
  ``period_end_inclusive`` is the closest one on-or-before ``today`` — or
  ``None`` if that cycle predates the card's own creation (backfill guard,
  design C1: don't alert on cycles that closed before the CC even existed
  in the system).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountType
from app.services.cc_cycle_service import CreditCardCycle, resolve_cycle_for_account


async def active_cc_accounts(db: AsyncSession, org_id: int) -> list[Account]:
    """Return the active, cycle-bearing credit-card accounts for one org.

    Filters (all required):
      - ``Account.org_id == org_id`` (security checkpoint — every caller of
        this helper MUST be operating within a single org's scope).
      - ``AccountType.slug == "credit_card"``.
      - ``Account.is_active.is_(True)``.
      - ``Account.close_day.isnot(None)`` — the CC-only cycle anchor;
        without it there's no cycle to resolve.
    """
    result = await db.execute(
        select(Account)
        .join(AccountType, Account.account_type_id == AccountType.id)
        .where(
            Account.org_id == org_id,
            AccountType.slug == "credit_card",
            Account.is_active.is_(True),
            Account.close_day.isnot(None),
        )
    )
    return list(result.scalars().all())


def most_recent_closed_cycle(account: object, today: date) -> Optional[CreditCardCycle]:
    """Resolve the most recently CLOSED cycle for ``account`` as of ``today``.

    ``resolve_cycle_for_account`` returns the cycle that CONTAINS
    ``today``. Whenever ``today`` sits after the last close but before the
    next one, that containing cycle is still open (``period_end_inclusive
    > today`` — today is in the post-close gap of the accumulating cycle).
    In that case, re-resolve one day before that cycle's ``period_start``.
    That always lands inside the PRIOR (already-closed) cycle. On the
    close day itself, ``period_end_inclusive == today`` (D2, inclusive
    close), so no adjustment is needed — the cycle IS closed today.

    Anchoring on ``cyc.period_start - timedelta(days=1)`` (not
    ``today - timedelta(days=1)``) is load-bearing: if ``today`` is 20
    days past the close, ``today - 1`` is still 19 days past close and
    resolves to the SAME upcoming cycle, not the prior closed one.
    """
    cyc = resolve_cycle_for_account(account, today)
    if cyc.period_end_inclusive > today:
        # today is in the post-close gap; step back into the prior cycle.
        cyc = resolve_cycle_for_account(account, cyc.period_start - timedelta(days=1))

    # Backfill guard (design C1): suppress cycles that closed on/before the
    # card's own creation date — those are backfill noise, not real closes.
    if cyc.period_end_inclusive <= account.created_at.date():
        return None

    return cyc
