"""Budget service — CRUD and spend computation.

Budgets are allocated at the master category level. Spend is computed
by summing settled expense transactions across all subcategories of
that master within the budget period.
"""

import datetime
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.budget import Budget
from app.models.category import Category
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization
from app.schemas.budget import BudgetCreate, BudgetResponse, BudgetUpdate
from app.services.exceptions import ConflictError, NotFoundError, ValidationError


def _current_period(billing_cycle_day: int) -> tuple[datetime.date, datetime.date]:
    """Compute the current billing period based on the org's cycle day."""
    today = datetime.date.today()
    y, m, d = today.year, today.month, today.day

    if d >= billing_cycle_day:
        start = datetime.date(y, m, billing_cycle_day)
        end_date = start + relativedelta(months=1) - datetime.timedelta(days=1)
    else:
        start = datetime.date(y, m, billing_cycle_day) - relativedelta(months=1)
        end_date = datetime.date(y, m, billing_cycle_day) - datetime.timedelta(days=1)

    return start, end_date


async def _get_billing_cycle_day(db: AsyncSession, org_id: int) -> int:
    result = await db.scalar(
        select(Organization.billing_cycle_day).where(Organization.id == org_id)
    )
    return result or 1


async def _compute_spent(
    db: AsyncSession, org_id: int, master_category_id: int,
    period_start: datetime.date, period_end: datetime.date,
) -> Decimal:
    """Sum settled expense transactions for a master category and all its subcategories."""
    # Get all subcategory IDs under this master
    sub_ids_result = await db.execute(
        select(Category.id).where(
            Category.parent_id == master_category_id, Category.org_id == org_id
        )
    )
    sub_ids = [r[0] for r in sub_ids_result.all()]
    # Include the master itself (for direct transactions)
    all_cat_ids = [master_category_id] + sub_ids

    spent = await db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.org_id == org_id,
            Transaction.category_id.in_(all_cat_ids),
            Transaction.type == TransactionType.EXPENSE,
            Transaction.status == TransactionStatus.SETTLED,
            Transaction.date >= period_start,
            Transaction.date <= period_end,
        )
    )
    return Decimal(str(spent))


def _to_response(budget: Budget, spent: Decimal) -> BudgetResponse:
    remaining = budget.amount - spent
    pct = float(spent / budget.amount * 100) if budget.amount > 0 else 0.0
    return BudgetResponse(
        id=budget.id,
        category_id=budget.category_id,
        category_name=budget.category.name if budget.category else "",
        amount=budget.amount,
        spent=spent,
        remaining=remaining,
        percent_used=round(pct, 1),
        period_start=budget.period_start,
        period_end=budget.period_end,
    )


# ── CRUD ──────────────────────────────────────────────────────────────────────

async def list_budgets(db: AsyncSession, org_id: int) -> list[BudgetResponse]:
    """List budgets for the current billing period with spend computation."""
    cycle_day = await _get_billing_cycle_day(db, org_id)
    period_start, period_end = _current_period(cycle_day)

    result = await db.execute(
        select(Budget)
        .where(
            Budget.org_id == org_id,
            Budget.period_start == period_start,
        )
        .order_by(Budget.category_id)
    )
    budgets = list(result.scalars().all())

    # Eager load categories
    for b in budgets:
        await db.refresh(b, ["category"])

    responses = []
    for b in budgets:
        spent = await _compute_spent(db, org_id, b.category_id, period_start, period_end)
        responses.append(_to_response(b, spent))

    return responses


async def create_budget(db: AsyncSession, org_id: int, body: BudgetCreate) -> BudgetResponse:
    """Create a budget for the current period. Only master categories allowed."""
    # Validate category is a master (no parent)
    cat_result = await db.execute(
        select(Category).where(Category.id == body.category_id, Category.org_id == org_id)
    )
    cat = cat_result.scalar_one_or_none()
    if cat is None:
        raise ValidationError("Invalid category")
    if cat.parent_id is not None:
        raise ValidationError("Budgets can only be set for master categories, not subcategories")

    cycle_day = await _get_billing_cycle_day(db, org_id)
    period_start, period_end = _current_period(cycle_day)

    # Check for existing budget
    existing = await db.scalar(
        select(Budget.id).where(
            Budget.org_id == org_id,
            Budget.category_id == body.category_id,
            Budget.period_start == period_start,
        )
    )
    if existing:
        raise ConflictError("Budget already exists for this category in the current period")

    budget = Budget(
        org_id=org_id,
        category_id=body.category_id,
        amount=body.amount,
        period_start=period_start,
        period_end=period_end,
    )
    db.add(budget)
    await db.commit()
    await db.refresh(budget, ["category"])

    spent = await _compute_spent(db, org_id, budget.category_id, period_start, period_end)
    return _to_response(budget, spent)


async def update_budget(
    db: AsyncSession, org_id: int, budget_id: int, body: BudgetUpdate
) -> BudgetResponse:
    result = await db.execute(
        select(Budget).where(Budget.id == budget_id, Budget.org_id == org_id)
    )
    budget = result.scalar_one_or_none()
    if budget is None:
        raise NotFoundError("Budget")

    if body.amount is not None:
        budget.amount = body.amount

    await db.commit()
    await db.refresh(budget, ["category"])

    spent = await _compute_spent(db, org_id, budget.category_id, budget.period_start, budget.period_end)
    return _to_response(budget, spent)


async def delete_budget(db: AsyncSession, org_id: int, budget_id: int) -> None:
    result = await db.execute(
        select(Budget).where(Budget.id == budget_id, Budget.org_id == org_id)
    )
    budget = result.scalar_one_or_none()
    if budget is None:
        raise NotFoundError("Budget")
    await db.delete(budget)
    await db.commit()
