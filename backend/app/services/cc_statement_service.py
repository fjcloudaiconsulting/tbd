"""Org-batched CC ledger loader + statement-outstanding helper.

CC Statement Alerts V1, Task 3. DB-facing counterpart to the pure math in
``cc_forecast_service`` (kept DB-free per its own docstring): this module
owns the query, ``cc_forecast_service`` owns the arithmetic.

``statement_outstanding`` computes a card's owed-at-close amount using the
EXACT same ledger reconstruction the Slice 3 forecast synthesis uses
(``account_balance_forecast_service.compute_account_balance_forecast``'s
credit-card block), so the close-day alert (Task 9) can never drift from
what the forecast bills for the same card and close date.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.transaction import Transaction, TransactionType
from app.services import cc_forecast_service as ccf
from app.services.transaction_filters import (
    balance_contribution_filter,
    effective_period_date_expr,
)


async def load_cc_ledgers(
    db: AsyncSession,
    org_id: int,
    account_ids: list[int],
    up_to: date,
) -> dict[int, list[tuple[date, Decimal]]]:
    """Signed cash-basis ledger per account, batched across ``account_ids``.

    Reproduces the ledger query used by the forecast's credit-card
    synthesis verbatim: a ``case``-signed amount (income +, expense -),
    ``effective_period_date_expr()`` for cash-basis bucketing,
    ``balance_contribution_filter()`` (the reciprocal-link discriminator
    that keeps real transfer legs but drops reconcile-matched reverted
    duplicates), filtered by ``Transaction.org_id == org_id`` -- and
    deliberately NO ``TransactionStatus`` clause, matching the forecast's
    "settled activity through today, pending activity too" reconstruction.

    ``up_to`` bounds the fetch to ``effective_date <= up_to``. This is a
    pure fetch-size optimization: ``cc_forecast_service.balance_at_close``
    already drops any row past whatever ``close_date`` it's called with,
    so narrowing the query here cannot change the result as long as every
    caller's ``close_date`` is <= ``up_to`` (true for both call sites: the
    single-card statement lookup passes its own close_date as up_to, and
    the forecast passes the horizon's period_end, which is >= every due
    cycle's close date within that horizon).

    Returns ``{}`` for an empty ``account_ids`` (no accounts to batch).
    """
    if not account_ids:
        return {}

    eff_date = effective_period_date_expr()
    signed = case(
        (Transaction.type == TransactionType.INCOME, Transaction.amount),
        else_=-Transaction.amount,
    )
    rows = (
        await db.execute(
            select(Transaction.account_id, eff_date.label("eff"), signed.label("signed"))
            .where(
                Transaction.org_id == org_id,
                Transaction.account_id.in_(account_ids),
                eff_date <= up_to,
                balance_contribution_filter(),
            )
        )
    ).all()

    ledgers: dict[int, list[tuple[date, Decimal]]] = {}
    for account_id, eff, s in rows:
        ledgers.setdefault(account_id, []).append((eff, Decimal(str(s))))
    return ledgers


async def statement_outstanding(
    db: AsyncSession,
    org_id: int,
    account: object,
    close_date: date,
) -> Decimal:
    """Positive amount owed on ``account`` as-of ``close_date``.

    Single-card convenience wrapper over ``load_cc_ledgers`` +
    ``cc_forecast_service.balance_at_close`` /
    ``cc_forecast_service.outstanding_at_close`` -- the same reconstruction
    the forecast uses, so the close-day alert amount matches the forecast
    exactly. 0 when the card is paid off or in credit.
    """
    ledgers = await load_cc_ledgers(db, org_id, [account.id], close_date)
    b_k = ccf.balance_at_close(
        Decimal(str(account.opening_balance)),
        ledgers.get(account.id, []),
        close_date,
    )
    return ccf.outstanding_at_close(b_k)
