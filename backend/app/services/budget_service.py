"""Budget service — CRUD and spend computation.

Budgets are allocated at the master category level per billing period.
Spend is computed by summing settled expense transactions across all
subcategories of that master within the period dates.
"""

import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.budget import Budget
from app.models.category import Category
from app.models.forecast_plan import ForecastItemType, ForecastPlan
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.schemas.budget import BudgetCreate, BudgetResponse, BudgetUpdate
from app.services.billing_service import get_current_period, resolve_period
from app.services.exceptions import ConflictError, NotFoundError, ValidationError
from app.services.transaction_filters import reportable_transaction_filter


async def _compute_spent(
    db: AsyncSession, org_id: int, master_category_id: int,
    period_start: datetime.date, period_end: datetime.date | None,
) -> Decimal:
    """Sum settled expense transactions for a master category and all its subcategories."""
    sub_ids_result = await db.execute(
        select(Category.id).where(
            Category.parent_id == master_category_id, Category.org_id == org_id
        )
    )
    sub_ids = [r[0] for r in sub_ids_result.all()]
    all_cat_ids = [master_category_id] + sub_ids

    # Use settled_date for budget computation — transactions count against
    # the billing period in which they settled, not when the purchase happened.
    # Transfer halves are persisted as type=expense with a non-null
    # linked_transaction_id; excluding them keeps budget spent aligned with
    # the dashboard donut when a transfer is tagged under a budgeted category.
    q = select(func.coalesce(func.sum(Transaction.amount), 0)).where(
        Transaction.org_id == org_id,
        Transaction.category_id.in_(all_cat_ids),
        Transaction.type == TransactionType.EXPENSE,
        Transaction.status == TransactionStatus.SETTLED,
        Transaction.settled_date >= period_start,
        reportable_transaction_filter(),
    )
    # If period is still open (no end_date), include all from start_date onward
    if period_end is not None:
        q = q.where(Transaction.settled_date <= period_end)

    spent = await db.scalar(q)
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

async def list_budgets(
    db: AsyncSession, org_id: int, period_start: datetime.date | None = None
) -> list[BudgetResponse]:
    """List budgets for a billing period with spend computation.
    If period_start is None, uses the current open period."""
    try:
        period = await resolve_period(db, org_id, period_start)
    except ValidationError:
        return []

    result = await db.execute(
        select(Budget)
        .where(
            Budget.org_id == org_id,
            Budget.period_start == period.start_date,
        )
        .order_by(Budget.category_id)
    )
    budgets = list(result.scalars().all())

    for b in budgets:
        await db.refresh(b, ["category"])

    responses = []
    for b in budgets:
        spent = await _compute_spent(db, org_id, b.category_id, period.start_date, period.end_date)
        responses.append(_to_response(b, spent))

    return responses


async def create_budget(
    db: AsyncSession, org_id: int, body: BudgetCreate,
    period_start: datetime.date | None = None,
) -> BudgetResponse:
    """Create a budget for a period. Only master categories allowed."""
    cat_result = await db.execute(
        select(Category).where(Category.id == body.category_id, Category.org_id == org_id)
    )
    cat = cat_result.scalar_one_or_none()
    if cat is None:
        raise ValidationError("Invalid category")
    if cat.parent_id is not None:
        raise ValidationError("Budgets can only be set for master categories, not subcategories")

    period = await resolve_period(db, org_id, period_start)

    existing = await db.scalar(
        select(Budget.id).where(
            Budget.org_id == org_id,
            Budget.category_id == body.category_id,
            Budget.period_start == period.start_date,
        )
    )
    if existing:
        raise ConflictError("Budget already exists for this category in the current period")

    budget = Budget(
        org_id=org_id,
        category_id=body.category_id,
        amount=body.amount,
        period_start=period.start_date,
        period_end=period.end_date,
    )
    db.add(budget)
    await db.commit()
    await db.refresh(budget, ["category"])

    spent = await _compute_spent(db, org_id, budget.category_id, period.start_date, period.end_date)
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

    # Use the live period end_date (not the stored one) for open periods
    period = await get_current_period(db, org_id)
    end = period.end_date if period.start_date == budget.period_start else budget.period_end
    spent = await _compute_spent(db, org_id, budget.category_id, budget.period_start, end)
    return _to_response(budget, spent)


async def transfer_budget(
    db: AsyncSession, org_id: int,
    from_budget_id: int, to_category_id: int, amount: Decimal,
) -> list[BudgetResponse]:
    """Transfer allocation from one budget to another within the same period.

    If the target category has no budget yet, one is created.
    Returns both the source and target budgets.
    """
    from sqlalchemy.exc import IntegrityError

    # Lock source budget for update to prevent concurrent over-allocation.
    # populate_existing=True enforces the codebase invariant that every FOR
    # UPDATE refreshes the ORM identity-map entry with the locked row state.
    result = await db.execute(
        select(Budget)
        .where(Budget.id == from_budget_id, Budget.org_id == org_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    source = result.scalar_one_or_none()
    if source is None:
        raise NotFoundError("Source budget")

    if amount > source.amount:
        raise ValidationError("Transfer amount exceeds source budget")

    # Validate target is a master category
    cat_result = await db.execute(
        select(Category).where(Category.id == to_category_id, Category.org_id == org_id)
    )
    target_cat = cat_result.scalar_one_or_none()
    if target_cat is None:
        raise ValidationError("Invalid target category")
    if target_cat.parent_id is not None:
        raise ValidationError("Target must be a master category")
    if target_cat.id == source.category_id:
        raise ValidationError("Cannot transfer to the same category")

    # Find or create target budget in same period
    target_result = await db.execute(
        select(Budget).where(
            Budget.org_id == org_id,
            Budget.category_id == to_category_id,
            Budget.period_start == source.period_start,
        )
    )
    target = target_result.scalar_one_or_none()

    if target is None:
        target = Budget(
            org_id=org_id,
            category_id=to_category_id,
            amount=amount,
            period_start=source.period_start,
            period_end=source.period_end,
        )
        db.add(target)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            # Re-lock source and re-fetch target after race. populate_existing
            # is required: rollback expires attributes but keeps the instance
            # in the identity map, so the re-lock must actively repopulate.
            result = await db.execute(
                select(Budget)
                .where(Budget.id == from_budget_id, Budget.org_id == org_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            source = result.scalar_one()
            if amount > source.amount:
                raise ValidationError("Transfer amount exceeds source budget")
            target_result = await db.execute(
                select(Budget).where(
                    Budget.org_id == org_id,
                    Budget.category_id == to_category_id,
                    Budget.period_start == source.period_start,
                )
            )
            target = target_result.scalar_one()
            target.amount += amount
    else:
        target.amount += amount

    source.amount -= amount

    await db.commit()
    await db.refresh(source, ["category"])
    await db.refresh(target, ["category"])

    period = await get_current_period(db, org_id)
    end = period.end_date if period.start_date == source.period_start else source.period_end

    source_spent = await _compute_spent(db, org_id, source.category_id, source.period_start, end)
    target_spent = await _compute_spent(db, org_id, target.category_id, target.period_start, end)

    return [_to_response(source, source_spent), _to_response(target, target_spent)]


async def delete_budget(db: AsyncSession, org_id: int, budget_id: int) -> None:
    result = await db.execute(
        select(Budget).where(Budget.id == budget_id, Budget.org_id == org_id)
    )
    budget = result.scalar_one_or_none()
    if budget is None:
        raise NotFoundError("Budget")
    await db.delete(budget)
    await db.commit()


async def _get_existing_budget_cat_ids(
    db: AsyncSession, org_id: int, period_start: datetime.date,
) -> set[int]:
    """Set of category_ids that already have a budget for the given period.

    Extracted so the race-handling regression test can monkey-patch this
    to simulate a stale read window (a concurrent caller inserted between
    our check and our commit). In production this is always called once
    inside create_budgets_from_forecast.
    """
    result = await db.execute(
        select(Budget.category_id).where(
            Budget.org_id == org_id,
            Budget.period_start == period_start,
        )
    )
    return {row[0] for row in result.all()}


async def create_budgets_from_forecast(
    db: AsyncSession, org_id: int,
) -> list[BudgetResponse]:
    """Copy expense items from the current period's forecast plan into
    Budget rows for the same period. Categories that already have a
    budget are skipped — calling this twice is a no-op on the second
    call.

    Raises ValidationError if no plan exists for the current period;
    the user is expected to create or copy one on the Forecasts page
    first. Returns the full budget list for the period.
    """
    period = await get_current_period(db, org_id)

    plan_result = await db.execute(
        select(ForecastPlan).where(
            ForecastPlan.org_id == org_id,
            ForecastPlan.billing_period_id == period.id,
        )
    )
    plan = plan_result.scalar_one_or_none()
    if plan is None:
        raise ValidationError(
            "No forecast plan exists for the current period. "
            "Create one on the Forecasts page first."
        )
    await db.refresh(plan, ["items"])

    existing_cat_ids = await _get_existing_budget_cat_ids(db, org_id, period.start_date)

    new_items = [
        item for item in plan.items
        if item.type == ForecastItemType.EXPENSE
        and item.category_id not in existing_cat_ids
    ]

    # Per-row savepoint so a concurrent caller that inserted the same
    # (org, category, period) row between our existing-check and our
    # commit only fails THAT row — not the whole batch. The DB's
    # uq_budget_org_cat_period constraint catches the duplicate; we
    # treat the IntegrityError as "the other request already did this
    # work" and move on. Same pattern as _get_or_create_plan_row in
    # forecast_plan_service.
    from sqlalchemy.exc import IntegrityError
    inserted_any = False
    for item in new_items:
        try:
            async with db.begin_nested():
                db.add(Budget(
                    org_id=org_id,
                    category_id=item.category_id,
                    amount=item.planned_amount,
                    period_start=period.start_date,
                    period_end=period.end_date,
                ))
                await db.flush()
            inserted_any = True
        except IntegrityError:
            # Concurrent insert beat us to this category — fine, skip.
            pass
    if inserted_any:
        await db.commit()

    return await list_budgets(db, org_id, period_start=period.start_date)
