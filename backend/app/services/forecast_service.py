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
from app.services.billing_service import get_current_period
from app.services.date_utils import advance_date


async def compute_forecast(
    db: AsyncSession, org_id: int, period_start: datetime.date | None = None
) -> dict:
    """Compute the full forecast for a billing period.

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

    p_start = period.start_date
    # For open periods, project to ~30 days from start
    p_end = period.end_date or (p_start + relativedelta(months=1) - datetime.timedelta(days=1))

    # ── Executed (settled) — uses settled_date for period assignment ─────
    # Transactions count against the period in which they settled,
    # not when the purchase happened (important for CC late settlements).
    executed_income = await db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.org_id == org_id,
            Transaction.type == TransactionType.INCOME,
            Transaction.status == TransactionStatus.SETTLED,
            Transaction.settled_date >= p_start,
            Transaction.settled_date <= p_end,
        )
    ) or Decimal("0")

    executed_expense = await db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.org_id == org_id,
            Transaction.type == TransactionType.EXPENSE,
            Transaction.status == TransactionStatus.SETTLED,
            Transaction.settled_date >= p_start,
            Transaction.settled_date <= p_end,
        )
    ) or Decimal("0")

    # ── Pending — uses transaction date (when purchase happened) ──────────
    pending_income = await db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.org_id == org_id,
            Transaction.type == TransactionType.INCOME,
            Transaction.status == TransactionStatus.PENDING,
            Transaction.date >= p_start,
            Transaction.date <= p_end,
        )
    ) or Decimal("0")

    pending_expense = await db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.org_id == org_id,
            Transaction.type == TransactionType.EXPENSE,
            Transaction.status == TransactionStatus.PENDING,
            Transaction.date >= p_start,
            Transaction.date <= p_end,
        )
    ) or Decimal("0")

    # ── Upcoming recurring (not yet generated for this period) ────────────
    today = datetime.date.today()
    result = await db.execute(
        select(RecurringTransaction).where(
            RecurringTransaction.org_id == org_id,
            RecurringTransaction.is_active == True,
            RecurringTransaction.next_due_date <= p_end,
            RecurringTransaction.next_due_date > today,
        )
    )
    recurring_items = list(result.scalars().all())

    recurring_income = Decimal("0")
    recurring_expense = Decimal("0")

    for r in recurring_items:
        d = r.next_due_date
        while d <= p_end:
            if r.type == "income":
                recurring_income += r.amount
            else:
                recurring_expense += r.amount
            d = advance_date(d, r.frequency)

    # ── Per-category breakdown ────────────────────────────────────────────
    # Executed by category (uses settled_date for period assignment)
    cat_exec_result = await db.execute(
        select(
            Transaction.category_id,
            func.sum(Transaction.amount),
        ).where(
            Transaction.org_id == org_id,
            Transaction.type == TransactionType.EXPENSE,
            Transaction.status == TransactionStatus.SETTLED,
            Transaction.settled_date >= p_start,
            Transaction.settled_date <= p_end,
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
            Transaction.date >= p_start,
            Transaction.date <= p_end,
        ).group_by(Transaction.category_id)
    )
    cat_pending = {row[0]: Decimal(str(row[1])) for row in cat_pend_result.all()}

    # Recurring by category
    cat_recurring: dict[int, Decimal] = {}
    for r in recurring_items:
        if r.type == "expense":
            d = r.next_due_date
            while d <= p_end:
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
        "period_end": p_end.isoformat(),
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
