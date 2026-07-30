"""Forecast service — compute projected month-end from executed + pending + recurring.

Forecast = Settled (what happened) + Pending (committed but not settled) + Upcoming Recurring (will be generated)

This gives the user a complete picture of where the month is heading.
"""

import datetime
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import BillingPeriod
from app.models.category import Category
from app.models.recurring import Frequency, RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.services.billing_service import get_current_period, period_spend_window_end
from app.services.date_utils import advance_date
from app.services.transaction_filters import (
    effective_period_date_expr,
    reportable_transaction_filter,
)


async def compute_forecast(
    db: AsyncSession,
    org_id: int,
    period_start: datetime.date | None = None,
    *,
    today: datetime.date | None = None,
) -> dict:
    """Compute the full forecast for a billing period.

    ``today`` is keyword-only and injectable because the period window floors
    at the wall clock (see ``window_end`` below). It is resolved ONCE here and
    every clock consumer in this function reads that same value — the recurring
    gate below used to call ``date.today()`` a second time, and two independent
    clock reads inside one computation is the straddle trap TBD-240 D6 exists
    to prevent. ``test_forecast_parity_after_generate`` asserts a conservation
    invariant ACROSS two calls; a bucket that moves because the clock ticked
    between two reads would break it.

    Returns:
        executed_income: sum of settled income
        executed_expense: sum of settled expenses
        pending_income: sum of pending income
        pending_expense: sum of pending expenses
        recurring_income: projected income from recurring templates
        recurring_expense: projected expenses from recurring templates
        forecast_income: executed + pending + recurring income
        forecast_expense: executed + pending + recurring expense
        forecast_net: forecast_income - forecast_expense
        executed_net: executed_income - executed_expense
        categories: per-category breakdown with executed + forecast
    """
    # Get the period
    if period_start:
        result = await db.execute(
            select(BillingPeriod).where(
                BillingPeriod.org_id == org_id,
                BillingPeriod.start_date == period_start,
            )
        )
        period = result.scalar_one_or_none()
        if period is None:
            period = await get_current_period(db, org_id)
    else:
        period = await get_current_period(db, org_id)

    today = today if today is not None else datetime.date.today()

    p_start = period.start_date

    # ── The period's ONE window (TBD-243) ─────────────────────────────────
    # `window_end` bounds EVERY backward sum and EVERY forward projection
    # horizon in this function. There is deliberately no second value and no
    # second name: the backward sum and the forward projection are two halves
    # of one total joined by a materialisation event, and
    # `generate_due_transactions` materialises on `current_cycle_window`,
    # which is ROSTER-INDEPENDENT. Two windows would open a gap the
    # materialisation window reaches into — an obligation in neither bucket
    # before and one bucket after — and `forecast_net` would move with no user
    # action. `specs/2026-07-30-forecast-period-window-design.md` §2
    # reproduces that break; §4 F4 fences it.
    #
    # Closed rows take `end_date` verbatim and are NEVER floored: flooring one
    # would re-open reported history for every org (§4 F6). The `None` arm is
    # the roster tail, where `period_effective_end` is genuinely unbounded;
    # the calendar expression there is FORCED, not chosen — the
    # `while d <= window_end` loops below cannot terminate on `None`, and it is
    # what keeps `period_end` non-null in the response contract (§4 F5).
    if period.end_date is not None:
        window_end = period.end_date
    else:
        derived = await period_spend_window_end(db, org_id, period, today=today)
        window_end = derived if derived is not None else (
            p_start + relativedelta(months=1) - datetime.timedelta(days=1)
        )

    # ── Executed (settled) — uses settled_date for period assignment ─────
    # Transactions count against the period in which they settled,
    # not when the purchase happened (important for CC late settlements).
    #
    # Transfer halves are persisted as type=income/expense with a non-null
    # linked_transaction_id; excluding them keeps executed/pending totals
    # aligned with the dashboard client-side aggregates and with how users
    # think about real income vs. real spending.
    executed_income = await db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.org_id == org_id,
            Transaction.type == TransactionType.INCOME,
            Transaction.status == TransactionStatus.SETTLED,
            Transaction.settled_date >= p_start,
            Transaction.settled_date <= window_end,
            reportable_transaction_filter(),
        )
    ) or Decimal("0")

    executed_expense = await db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.org_id == org_id,
            Transaction.type == TransactionType.EXPENSE,
            Transaction.status == TransactionStatus.SETTLED,
            Transaction.settled_date >= p_start,
            Transaction.settled_date <= window_end,
            reportable_transaction_filter(),
        )
    ) or Decimal("0")

    # ── Pending — buckets by effective settled date (the settled-date
    # estimate when set, else purchase date) so pending rows fall in the
    # period they're expected to clear, consistent with the list/reports.
    pending_income = await db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.org_id == org_id,
            Transaction.type == TransactionType.INCOME,
            Transaction.status == TransactionStatus.PENDING,
            effective_period_date_expr() >= p_start,
            effective_period_date_expr() <= window_end,
            reportable_transaction_filter(),
        )
    ) or Decimal("0")

    pending_expense = await db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.org_id == org_id,
            Transaction.type == TransactionType.EXPENSE,
            Transaction.status == TransactionStatus.PENDING,
            effective_period_date_expr() >= p_start,
            effective_period_date_expr() <= window_end,
            reportable_transaction_filter(),
        )
    ) or Decimal("0")

    # ── Upcoming recurring (not yet generated for this period) ────────────
    # `today` is the ONE value resolved at the top of this function. Do not
    # re-read the clock here.
    result = await db.execute(
        select(RecurringTransaction).where(
            RecurringTransaction.org_id == org_id,
            RecurringTransaction.is_active == True,
            RecurringTransaction.next_due_date <= window_end,
            RecurringTransaction.next_due_date > today,
        )
    )
    recurring_items = list(result.scalars().all())

    recurring_income = Decimal("0")
    recurring_expense = Decimal("0")

    for r in recurring_items:
        d = r.next_due_date
        while d <= window_end:
            if r.type == "income":
                recurring_income += r.amount
            else:
                recurring_expense += r.amount
            d = advance_date(d, r.frequency)

    # ── Per-category breakdown ────────────────────────────────────────────
    # Executed by category (uses settled_date for period assignment).
    # Exclude transfer halves so Forecast by Category matches the
    # dashboard donut.
    cat_exec_result = await db.execute(
        select(
            Transaction.category_id,
            func.sum(Transaction.amount),
        ).where(
            Transaction.org_id == org_id,
            Transaction.type == TransactionType.EXPENSE,
            Transaction.status == TransactionStatus.SETTLED,
            Transaction.settled_date >= p_start,
            Transaction.settled_date <= window_end,
            reportable_transaction_filter(),
        ).group_by(Transaction.category_id)
    )
    cat_executed = {row[0]: Decimal(str(row[1])) for row in cat_exec_result.all()}

    # Pending by category
    cat_pend_result = await db.execute(
        select(
            Transaction.category_id,
            func.sum(Transaction.amount),
        ).where(
            Transaction.org_id == org_id,
            Transaction.type == TransactionType.EXPENSE,
            Transaction.status == TransactionStatus.PENDING,
            effective_period_date_expr() >= p_start,
            effective_period_date_expr() <= window_end,
            reportable_transaction_filter(),
        ).group_by(Transaction.category_id)
    )
    cat_pending = {row[0]: Decimal(str(row[1])) for row in cat_pend_result.all()}

    # Recurring by category
    cat_recurring: dict[int, Decimal] = {}
    for r in recurring_items:
        if r.type == "expense":
            d = r.next_due_date
            while d <= window_end:
                cat_recurring[r.category_id] = cat_recurring.get(r.category_id, Decimal("0")) + r.amount
                d = advance_date(d, r.frequency)

    # Merge all category IDs
    all_cat_ids = set(cat_executed.keys()) | set(cat_pending.keys()) | set(cat_recurring.keys())

    # Get category names
    cat_names = {}
    if all_cat_ids:
        name_result = await db.execute(
            select(Category.id, Category.name, Category.parent_id).where(
                Category.id.in_(all_cat_ids), Category.org_id == org_id
            )
        )
        for row in name_result.all():
            cat_names[row[0]] = {"name": row[1], "parent_id": row[2]}

    categories = []
    for cid in sorted(all_cat_ids):
        ex = cat_executed.get(cid, Decimal("0"))
        pe = cat_pending.get(cid, Decimal("0"))
        rc = cat_recurring.get(cid, Decimal("0"))
        info = cat_names.get(cid, {"name": "Unknown", "parent_id": None})
        categories.append({
            "category_id": cid,
            "category_name": info["name"],
            "parent_id": info["parent_id"],
            "executed": str(ex),
            "pending": str(pe),
            "recurring": str(rc),
            "forecast": str(ex + pe + rc),
        })

    # ── Totals ────────────────────────────────────────────────────────────
    forecast_income = executed_income + pending_income + recurring_income
    forecast_expense = executed_expense + pending_expense + recurring_expense

    return {
        "period_start": p_start.isoformat(),
        "period_end": window_end.isoformat(),
        "executed_income": str(executed_income),
        "executed_expense": str(executed_expense),
        "executed_net": str(executed_income - executed_expense),
        "pending_income": str(pending_income),
        "pending_expense": str(pending_expense),
        "recurring_income": str(recurring_income),
        "recurring_expense": str(recurring_expense),
        "forecast_income": str(forecast_income),
        "forecast_expense": str(forecast_expense),
        "forecast_net": str(forecast_income - forecast_expense),
        "categories": categories,
    }
