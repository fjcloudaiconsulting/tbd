"""Per-account expected month-end balance projection.

Dashboard-only view. Distinct from forecast_service.compute_forecast,
which deals with reportable income/expense aggregates (and excludes
transfer legs / manual adjustments). This module answers a different
question:

  "What will each account's balance be at the end of this billing period?"

Account balance is the sum of all settled transactions on the account,
including transfer legs. Pending transactions on the account haven't
moved the stored balance yet but will. So the projection is simply:

  expected_account_balance = stored_balance + sum(pending deltas in period)

with sign by type (income +, expense -). Transfer legs MUST be included
because they DO move balances per-account, even though they're not
reportable income/expense. Manual adjustments are settled-only by design,
but we filter them out defensively from the pending delta in case that
ever changes.

Currency totals are grouped: never sum unlike currencies.
"""

import datetime
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountType
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.services.billing_service import resolve_period
from app.services.transaction_filters import effective_period_date_expr


async def compute_account_balance_forecast(
    db: AsyncSession,
    org_id: int,
    *,
    period_start: datetime.date | None = None,
) -> dict:
    """Compute expected month-end balance per account for a billing period.

    Returns the spec shape:

        {
          "period_start": "YYYY-MM-DD",
          "period_end": "YYYY-MM-DD",
          "totals": [{currency, balance, pending_delta, expected_month_end_balance}],
          "accounts": [{account_id, account_name, currency, is_default,
                        account_type_slug, balance, pending_delta,
                        expected_month_end_balance}],
        }
    """
    period = await resolve_period(db, org_id, period_start)

    p_start = period.start_date
    p_end = period.end_date or (
        p_start + relativedelta(months=1) - datetime.timedelta(days=1)
    )

    accounts_result = await db.execute(
        select(Account, AccountType.slug)
        .join(AccountType, Account.account_type_id == AccountType.id)
        .where(
            Account.org_id == org_id,
            Account.is_active.is_(True),
        )
    )
    rows = accounts_result.all()

    # Aggregate pending transactions in the selected period by account.
    # Sign by type: income +, expense -. Include transfer legs (this is
    # per-account balance math, not reportable). Defensively exclude
    # manual adjustments (settled-only today).
    eff_date = effective_period_date_expr()
    pending_result = await db.execute(
        select(
            Transaction.account_id,
            Transaction.type,
            func.coalesce(func.sum(Transaction.amount), Decimal("0")),
        )
        .where(
            Transaction.org_id == org_id,
            Transaction.status == TransactionStatus.PENDING,
            Transaction.is_manual_adjustment.is_(False),
            and_(eff_date >= p_start, eff_date <= p_end),
        )
        .group_by(Transaction.account_id, Transaction.type)
    )

    pending_by_account: dict[int, Decimal] = {}
    for account_id, tx_type, total in pending_result.all():
        delta = Decimal(str(total or 0))
        if tx_type == TransactionType.EXPENSE:
            delta = -delta
        pending_by_account[account_id] = (
            pending_by_account.get(account_id, Decimal("0")) + delta
        )

    accounts_payload: list[dict] = []
    totals_by_currency: dict[str, dict[str, Decimal]] = {}

    sorted_rows = sorted(
        rows,
        key=lambda r: (
            not r[0].is_default,
            r[0].name.casefold(),
            r[0].id,
        ),
    )

    for account, type_slug in sorted_rows:
        balance = Decimal(str(account.balance))
        delta = pending_by_account.get(account.id, Decimal("0"))
        expected = balance + delta

        accounts_payload.append(
            {
                "account_id": account.id,
                "account_name": account.name,
                "currency": account.currency,
                "is_default": account.is_default,
                "account_type_slug": type_slug,
                "balance": _q(balance),
                "pending_delta": _q(delta),
                "expected_month_end_balance": _q(expected),
            }
        )

        bucket = totals_by_currency.setdefault(
            account.currency,
            {"balance": Decimal("0"), "pending_delta": Decimal("0")},
        )
        bucket["balance"] += balance
        bucket["pending_delta"] += delta

    totals_payload = [
        {
            "currency": currency,
            "balance": _q(b["balance"]),
            "pending_delta": _q(b["pending_delta"]),
            "expected_month_end_balance": _q(b["balance"] + b["pending_delta"]),
        }
        for currency, b in sorted(totals_by_currency.items())
    ]

    return {
        "period_start": p_start.isoformat(),
        "period_end": p_end.isoformat(),
        "totals": totals_payload,
        "accounts": accounts_payload,
    }


_TWOPLACES = Decimal("0.01")


def _q(value: Decimal) -> str:
    """Format a Decimal as a fixed 2-decimal string for JSON transport."""
    return str(value.quantize(_TWOPLACES))
