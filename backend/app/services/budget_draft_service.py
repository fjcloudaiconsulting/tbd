"""Deterministic next-period budget draft.

Proposes a budget for each expense category from its trailing 3-complete-
month spend average. The next period has no actuals, so the projection is
simply the average (via the shared ``_project_period_spend`` helper). No
LLM dispatch and no ``ai.budget`` entitlement gate — this is pure
arithmetic over aggregates. Applying a draft CREATES budget rows (every
suggestion has ``current_amount == 0``).
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import structlog
from dateutil.relativedelta import relativedelta
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.budget import Budget
from app.models.category import Category, CategoryType
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.schemas.budget_draft import BudgetDraftResponse
from app.schemas.budget_rebalance import BudgetDeltaSuggestion
from app.services.billing_service import ensure_future_periods, resolve_period
from app.services.budget_rebalance_service import (
    CENT,
    _CategoryFact,
    _project_period_spend,
)
from app.services.transaction_filters import (
    effective_period_date_expr,
    reportable_transaction_filter,
)

logger = structlog.stdlib.get_logger()


async def _gather_draft_facts(
    db: AsyncSession, org_id: int, period_start: datetime.date
) -> list[_CategoryFact]:
    """Per-master-expense-category trailing 3-month spend facts.

    Rolls each master category's own settled expense together with its
    subcategories' (a fresh draft has no per-child budgets, so a simple
    master+subs rollup can't double-count). The window is the 3 complete
    calendar months before the current month (mirrors the rebalance
    ``_gather_facts`` window), and ``current_mo_actual`` is 0 because the
    drafted period has not started. Aggregates only — no transaction-level
    data leaves this function.
    """
    masters = (
        await db.execute(
            select(Category.id, Category.name)
            .where(
                Category.org_id == org_id,
                Category.type == CategoryType.EXPENSE,
                Category.parent_id.is_(None),
            )
            .order_by(Category.id)
        )
    ).all()
    if not masters:
        return []
    master_ids = [m[0] for m in masters]
    names = {m[0]: m[1] for m in masters}

    sub_rows = (
        await db.execute(
            select(Category.id, Category.parent_id).where(
                Category.org_id == org_id,
                Category.parent_id.in_(master_ids),
            )
        )
    ).all()
    parent_to_subs: dict[int, list[int]] = {mid: [] for mid in master_ids}
    all_ids: set[int] = set(master_ids)
    for sub_id, parent_id in sub_rows:
        parent_to_subs.setdefault(parent_id, []).append(sub_id)
        all_ids.add(sub_id)

    today = datetime.date.today()
    current_month_start = today.replace(day=1)
    three_mo_upper = min(current_month_start, period_start)
    three_mo_lower = three_mo_upper - relativedelta(months=3)

    rows = (
        await db.execute(
            select(Transaction.category_id, func.sum(Transaction.amount))
            .where(
                Transaction.org_id == org_id,
                Transaction.category_id.in_(all_ids),
                Transaction.type == TransactionType.EXPENSE,
                Transaction.status == TransactionStatus.SETTLED,
                reportable_transaction_filter(),
                effective_period_date_expr() >= three_mo_lower,
                effective_period_date_expr() < three_mo_upper,
            )
            .group_by(Transaction.category_id)
        )
    ).all()
    spend_by_cat: dict[int, Decimal] = {
        cid: Decimal(str(total or 0)) for cid, total in rows
    }

    facts: list[_CategoryFact] = []
    for mid in master_ids:
        total = spend_by_cat.get(mid, Decimal(0))
        for sub in parent_to_subs.get(mid, []):
            total += spend_by_cat.get(sub, Decimal(0))
        avg = (total / Decimal(3)).quantize(CENT)
        facts.append(
            _CategoryFact(
                category_id=mid,
                category_name=names[mid],
                budget_amount=Decimal("0"),
                last_3mo_total=total,
                last_3mo_avg=avg,
                current_mo_actual=Decimal("0"),
            )
        )
    return facts


async def suggest_next_period_budget(
    db: AsyncSession, org_id: int, *, period_start: datetime.date
) -> BudgetDraftResponse:
    """Draft budgets for the given (next) period from trailing spend.

    One suggestion per expense category with spend history, skipping any
    category that already has a budget in the target period. Returns
    ``empty_no_history`` when nothing can be drafted.
    """
    await ensure_future_periods(db, org_id=org_id)
    period = await resolve_period(db, org_id, period_start)

    facts = await _gather_draft_facts(db, org_id, period.start_date)

    existing_rows = (
        await db.execute(
            select(Budget.category_id).where(
                Budget.org_id == org_id,
                Budget.period_start == period.start_date,
            )
        )
    ).all()
    existing_ids = {r[0] for r in existing_rows}

    suggestions: list[BudgetDeltaSuggestion] = []
    for f in facts:
        if f.category_id in existing_ids:
            continue
        projected = _project_period_spend(f)  # == last_3mo_avg (actual is 0)
        if projected <= 0:
            continue
        suggestions.append(
            BudgetDeltaSuggestion(
                category_id=f.category_id,
                category_name=f.category_name,
                current_amount=Decimal("0.00"),
                suggested_amount=projected,
                delta_amount=projected,
                reasoning=(
                    f"Based on about {projected:.2f} per month over the "
                    "last 3 months."
                ),
            )
        )

    if not suggestions:
        return BudgetDraftResponse(
            status="empty_no_history",
            period_start=period.start_date,
            summary=(
                "Not enough recent spending history to draft a budget for "
                "this period. Add a few months of categorized transactions "
                "first, or seed from a forecast plan."
            ),
        )

    return BudgetDraftResponse(
        status="ok",
        period_start=period.start_date,
        suggestions=suggestions,
        summary="Draft budgets projected from your last 3 months of spending.",
    )
