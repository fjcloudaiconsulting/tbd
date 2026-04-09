"""Forecast plan service — editable plans for income/expense per billing period.

Users can create a plan for a billing period, auto-populate from recurring
templates and historical averages, then manually adjust. The plan tracks
actual vs planned for each line item.
"""

import datetime
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from sqlalchemy import func, literal_column, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import BillingPeriod
from app.models.category import Category
from app.models.forecast_plan import (
    ForecastItemType,
    ForecastPlan,
    ForecastPlanItem,
    ItemSource,
    PlanStatus,
)
from app.models.recurring import RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.schemas.forecast_plan import (
    BulkUpsertRequest,
    ForecastPlanItemCreate,
    ForecastPlanItemResponse,
    ForecastPlanItemUpdate,
    ForecastPlanResponse,
)
from app.services.billing_service import get_current_period
from app.services.exceptions import ConflictError, NotFoundError, ValidationError
from app.services.forecast_service import _advance_date


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _resolve_period(
    db: AsyncSession, org_id: int, period_start: datetime.date | None
) -> BillingPeriod:
    if period_start:
        result = await db.execute(
            select(BillingPeriod).where(
                BillingPeriod.org_id == org_id,
                BillingPeriod.start_date == period_start,
            )
        )
        period = result.scalar_one_or_none()
        if period is None:
            raise ValidationError("Billing period not found")
        return period
    return await get_current_period(db, org_id)


async def _get_or_create_plan_row(
    db: AsyncSession, org_id: int, period_id: int,
) -> ForecastPlan:
    """Get or create a plan row, handling concurrent insert races."""
    result = await db.execute(
        select(ForecastPlan).where(
            ForecastPlan.org_id == org_id,
            ForecastPlan.billing_period_id == period_id,
        )
    )
    plan = result.scalar_one_or_none()
    if plan is not None:
        return plan

    plan = ForecastPlan(
        org_id=org_id,
        billing_period_id=period_id,
        status=PlanStatus.DRAFT,
    )
    db.add(plan)
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError:
        result = await db.execute(
            select(ForecastPlan).where(
                ForecastPlan.org_id == org_id,
                ForecastPlan.billing_period_id == period_id,
            )
        )
        plan = result.scalar_one_or_none()
        if plan is None:
            raise ValidationError("Failed to create forecast plan")
    return plan


def _require_draft(plan: ForecastPlan) -> None:
    """Raise if the plan is active (read-only)."""
    if plan.status == PlanStatus.ACTIVE:
        raise ValidationError("Cannot modify an active plan. Revert to draft first.")


async def _validate_master_category(
    db: AsyncSession, org_id: int, category_id: int,
) -> None:
    """Validate that the category exists, belongs to the org, and is a master category."""
    result = await db.execute(
        select(Category).where(Category.id == category_id, Category.org_id == org_id)
    )
    cat = result.scalar_one_or_none()
    if cat is None:
        raise ValidationError("Invalid category")
    if cat.parent_id is not None:
        raise ValidationError("Forecast plan items must use master categories, not subcategories")


async def _compute_actuals_batch(
    db: AsyncSession, org_id: int,
    items: list[ForecastPlanItem],
    period_start: datetime.date, period_end: datetime.date | None,
) -> dict[tuple[int, str], Decimal]:
    """Compute actual amounts for all plan items in two queries (income + expense).

    Returns a dict keyed by (category_id, type_value) → actual amount.
    Each category includes its subcategories in the sum.
    """
    if not items:
        return {}

    # Collect all master category IDs from plan items
    master_ids = {item.category_id for item in items}

    # Build mapping: master_id → [master_id, sub1, sub2, ...]
    sub_result = await db.execute(
        select(Category.id, Category.parent_id).where(
            Category.parent_id.in_(master_ids), Category.org_id == org_id
        )
    )
    cat_to_master: dict[int, int] = {}
    for cat_id, parent_id in sub_result.all():
        cat_to_master[cat_id] = parent_id
    # Masters map to themselves
    for mid in master_ids:
        cat_to_master[mid] = mid

    all_cat_ids = list(cat_to_master.keys())

    # Single query: sum by (category_id, type) for all relevant categories
    q = select(
        Transaction.category_id,
        Transaction.type,
        func.coalesce(func.sum(Transaction.amount), 0),
    ).where(
        Transaction.org_id == org_id,
        Transaction.category_id.in_(all_cat_ids),
        Transaction.status == TransactionStatus.SETTLED,
        Transaction.date >= period_start,
        Transaction.type.in_(["income", "expense"]),
    )
    if period_end is not None:
        q = q.where(Transaction.date <= period_end)
    q = q.group_by(Transaction.category_id, Transaction.type)

    result = await db.execute(q)

    # Aggregate to master category level
    actuals: dict[tuple[int, str], Decimal] = {}
    for cat_id, tx_type_raw, amount in result.all():
        tx_type = tx_type_raw.value if hasattr(tx_type_raw, "value") else str(tx_type_raw)
        master_id = cat_to_master.get(cat_id, cat_id)
        key = (master_id, tx_type)
        actuals[key] = actuals.get(key, Decimal("0")) + Decimal(str(amount))

    return actuals


async def _build_response(
    db: AsyncSession, org_id: int, plan: ForecastPlan,
) -> ForecastPlanResponse:
    period = plan.billing_period
    p_start = period.start_date
    p_end = period.end_date

    # Batch compute actuals for all items (2 queries instead of 2*N)
    actuals = await _compute_actuals_batch(db, org_id, plan.items, p_start, p_end)

    # Batch fetch category names (avoids lazy-load MissingGreenlet in async)
    cat_ids = {item.category_id for item in plan.items}
    cat_info: dict[int, tuple[str, int | None]] = {}
    if cat_ids:
        cat_result = await db.execute(
            select(Category.id, Category.name, Category.parent_id).where(
                Category.id.in_(cat_ids), Category.org_id == org_id
            )
        )
        for cid, cname, pid in cat_result.all():
            cat_info[cid] = (cname, pid)

    item_responses = []
    total_planned_income = Decimal("0")
    total_planned_expense = Decimal("0")
    total_actual_income = Decimal("0")
    total_actual_expense = Decimal("0")

    for item in plan.items:
        actual = actuals.get((item.category_id, item.type.value), Decimal("0"))
        cname, pid = cat_info.get(item.category_id, ("Unknown", None))
        resp = ForecastPlanItemResponse(
            id=item.id,
            plan_id=item.plan_id,
            category_id=item.category_id,
            category_name=cname,
            parent_id=pid,
            type=item.type.value,
            planned_amount=item.planned_amount,
            source=item.source.value,
            actual_amount=actual,
            variance=actual - item.planned_amount,
        )
        item_responses.append(resp)

        if item.type == ForecastItemType.INCOME:
            total_planned_income += item.planned_amount
            total_actual_income += actual
        else:
            total_planned_expense += item.planned_amount
            total_actual_expense += actual

    return ForecastPlanResponse(
        id=plan.id,
        billing_period_id=plan.billing_period_id,
        period_start=p_start,
        period_end=p_end,
        status=plan.status.value,
        total_planned_income=total_planned_income,
        total_planned_expense=total_planned_expense,
        total_actual_income=total_actual_income,
        total_actual_expense=total_actual_expense,
        items=item_responses,
    )


# ── CRUD ─────────────────────────────────────────────────────────────────────

async def get_or_create_plan(
    db: AsyncSession, org_id: int, period_start: datetime.date | None = None,
) -> ForecastPlanResponse:
    """Get existing plan for a period, or create a new draft."""
    period = await _resolve_period(db, org_id, period_start)
    plan = await _get_or_create_plan_row(db, org_id, period.id)
    await db.commit()
    await db.refresh(plan, ["billing_period", "items"])
    return await _build_response(db, org_id, plan)


async def populate_from_sources(
    db: AsyncSession, org_id: int, period_start: datetime.date | None = None,
) -> ForecastPlanResponse:
    """Auto-populate plan items from recurring templates and 3-month history averages.

    Only adds items for categories not already in the plan.
    """
    period = await _resolve_period(db, org_id, period_start)
    plan = await _get_or_create_plan_row(db, org_id, period.id)
    await db.refresh(plan, ["billing_period", "items"])

    # Existing (category_id, type_str) combos — always use strings for consistency
    existing_keys: set[tuple[int, str]] = {(i.category_id, i.type.value) for i in plan.items}

    p_start = period.start_date
    p_end = period.end_date or (p_start + relativedelta(months=1) - datetime.timedelta(days=1))

    # ── From active recurring templates (with date filter) ──
    rec_result = await db.execute(
        select(RecurringTransaction).where(
            RecurringTransaction.org_id == org_id,
            RecurringTransaction.is_active == True,
            RecurringTransaction.next_due_date <= p_end,
        )
    )
    for r in rec_result.scalars().all():
        key = (r.category_id, r.type)  # r.type is str on RecurringTransaction
        if key in existing_keys:
            continue

        # Count occurrences within the period — advance to period start first
        total = Decimal("0")
        d = r.next_due_date
        # Fast-forward past dates before the period to avoid unnecessary iterations
        while d < p_start and d <= p_end:
            d = _advance_date(d, r.frequency)
        while d <= p_end:
            total += r.amount
            prev = d
            d = _advance_date(d, r.frequency)
            if d <= prev:
                break  # safety: prevent infinite loop on bad frequency

        if total > 0:
            item = ForecastPlanItem(
                plan_id=plan.id,
                org_id=org_id,
                category_id=r.category_id,
                type=ForecastItemType(r.type),
                planned_amount=total,
                source=ItemSource.RECURRING,
            )
            db.add(item)
            existing_keys.add(key)

    # ── From 3-month historical monthly averages ──
    three_months_ago = p_start - relativedelta(months=3)

    # Subquery: sum per category per type per month
    monthly_sub = (
        select(
            Transaction.category_id,
            Transaction.type,
            func.date_format(Transaction.date, "%Y-%m").label("month"),
            func.sum(Transaction.amount).label("monthly_total"),
        )
        .where(
            Transaction.org_id == org_id,
            Transaction.status == TransactionStatus.SETTLED,
            Transaction.date >= three_months_ago,
            Transaction.date < p_start,
            Transaction.type.in_(["income", "expense"]),
        )
        .group_by(Transaction.category_id, Transaction.type, text("month"))
        .subquery()
    )

    # Average the monthly totals
    hist_result = await db.execute(
        select(
            monthly_sub.c.category_id,
            monthly_sub.c.type,
            func.avg(monthly_sub.c.monthly_total),
            func.count(literal_column("*")),
        ).group_by(monthly_sub.c.category_id, monthly_sub.c.type)
    )

    for cat_id, tx_type_raw, avg_monthly, month_count in hist_result.all():
        # Normalize tx_type to string (may come back as enum or str depending on driver)
        tx_type = tx_type_raw.value if hasattr(tx_type_raw, "value") else str(tx_type_raw)

        key = (cat_id, tx_type)
        if key in existing_keys:
            continue
        if month_count < 2:  # Need at least 2 months to suggest
            continue

        # Resolve to master category for the plan item
        cat_result = await db.execute(
            select(Category).where(Category.id == cat_id, Category.org_id == org_id)
        )
        cat = cat_result.scalar_one_or_none()
        if cat is None:
            continue

        master_id = cat.parent_id if cat.parent_id else cat.id
        master_key = (master_id, tx_type)
        if master_key in existing_keys:
            continue

        item = ForecastPlanItem(
            plan_id=plan.id,
            org_id=org_id,
            category_id=master_id,
            type=ForecastItemType(tx_type),
            planned_amount=Decimal(str(round(float(avg_monthly), 2))),
            source=ItemSource.HISTORY,
        )
        db.add(item)
        existing_keys.add(master_key)

    await db.commit()
    await db.refresh(plan, ["billing_period", "items"])
    return await _build_response(db, org_id, plan)


async def upsert_item(
    db: AsyncSession, org_id: int, plan_id: int, body: ForecastPlanItemCreate,
) -> ForecastPlanResponse:
    """Add or update a single plan item."""
    plan = await _get_plan(db, org_id, plan_id)
    _require_draft(plan)

    await _validate_master_category(db, org_id, body.category_id)

    # Find existing item
    existing = None
    for item in plan.items:
        if item.category_id == body.category_id and item.type.value == body.type:
            existing = item
            break

    if existing:
        existing.planned_amount = body.planned_amount
        existing.source = ItemSource(body.source)
    else:
        new_item = ForecastPlanItem(
            plan_id=plan.id,
            org_id=org_id,
            category_id=body.category_id,
            type=ForecastItemType(body.type),
            planned_amount=body.planned_amount,
            source=ItemSource(body.source),
        )
        db.add(new_item)

    await db.commit()
    await db.refresh(plan, ["billing_period", "items"])
    return await _build_response(db, org_id, plan)


async def bulk_upsert(
    db: AsyncSession, org_id: int, plan_id: int, body: BulkUpsertRequest,
) -> ForecastPlanResponse:
    """Bulk add/update multiple plan items at once."""
    plan = await _get_plan(db, org_id, plan_id)
    _require_draft(plan)

    # Validate all category IDs belong to the org and are master categories
    requested_ids = {item.category_id for item in body.items}
    if requested_ids:
        valid_result = await db.execute(
            select(Category.id).where(
                Category.id.in_(requested_ids),
                Category.org_id == org_id,
                Category.parent_id.is_(None),
            )
        )
        valid_ids = {r[0] for r in valid_result.all()}
        invalid = requested_ids - valid_ids
        if invalid:
            raise ValidationError(f"Invalid or non-master category IDs: {sorted(invalid)}")

    existing_map = {
        (i.category_id, i.type.value): i for i in plan.items
    }

    for item_data in body.items:
        key = (item_data.category_id, item_data.type)
        if key in existing_map:
            existing_map[key].planned_amount = item_data.planned_amount
            existing_map[key].source = ItemSource(item_data.source)
        else:
            new_item = ForecastPlanItem(
                plan_id=plan.id,
                org_id=org_id,
                category_id=item_data.category_id,
                type=ForecastItemType(item_data.type),
                planned_amount=item_data.planned_amount,
                source=ItemSource(item_data.source),
            )
            db.add(new_item)

    await db.commit()
    await db.refresh(plan, ["billing_period", "items"])
    return await _build_response(db, org_id, plan)


async def update_item(
    db: AsyncSession, org_id: int, plan_id: int, item_id: int, body: ForecastPlanItemUpdate,
) -> ForecastPlanResponse:
    """Update a single plan item amount."""
    plan = await _get_plan(db, org_id, plan_id)
    _require_draft(plan)

    item = None
    for i in plan.items:
        if i.id == item_id:
            item = i
            break

    if item is None:
        raise NotFoundError("Forecast plan item")

    item.planned_amount = body.planned_amount
    item.source = ItemSource.MANUAL

    await db.commit()
    await db.refresh(plan, ["billing_period", "items"])
    return await _build_response(db, org_id, plan)


async def delete_item(
    db: AsyncSession, org_id: int, plan_id: int, item_id: int,
) -> ForecastPlanResponse:
    """Remove a single plan item."""
    plan = await _get_plan(db, org_id, plan_id)
    _require_draft(plan)

    item = None
    for i in plan.items:
        if i.id == item_id:
            item = i
            break

    if item is None:
        raise NotFoundError("Forecast plan item")

    await db.delete(item)
    await db.commit()
    await db.refresh(plan, ["billing_period", "items"])
    return await _build_response(db, org_id, plan)


async def activate_plan(
    db: AsyncSession, org_id: int, plan_id: int,
) -> ForecastPlanResponse:
    """Mark plan as active (finalized). Active plans are read-only."""
    plan = await _get_plan(db, org_id, plan_id)

    if not plan.items:
        raise ValidationError("Cannot activate an empty plan")

    plan.status = PlanStatus.ACTIVE
    await db.commit()
    await db.refresh(plan, ["billing_period", "items"])
    return await _build_response(db, org_id, plan)


async def revert_to_draft(
    db: AsyncSession, org_id: int, plan_id: int,
) -> ForecastPlanResponse:
    """Revert an active plan back to draft for editing."""
    plan = await _get_plan(db, org_id, plan_id)

    if plan.status != PlanStatus.ACTIVE:
        raise ValidationError("Plan is already a draft")

    plan.status = PlanStatus.DRAFT
    await db.commit()
    await db.refresh(plan, ["billing_period", "items"])
    return await _build_response(db, org_id, plan)


async def discard_plan(
    db: AsyncSession, org_id: int, plan_id: int,
) -> ForecastPlanResponse:
    """Remove all items from a draft plan."""
    plan = await _get_plan(db, org_id, plan_id)
    _require_draft(plan)

    for item in list(plan.items):
        await db.delete(item)

    plan.status = PlanStatus.DRAFT
    await db.commit()
    await db.refresh(plan, ["billing_period", "items"])
    return await _build_response(db, org_id, plan)


async def copy_from_period(
    db: AsyncSession, org_id: int,
    target_period_start: datetime.date | None,
    source_period_start: datetime.date,
) -> ForecastPlanResponse:
    """Copy plan items from a previous period to the target period."""
    target_period = await _resolve_period(db, org_id, target_period_start)
    source_period = await _resolve_period(db, org_id, source_period_start)

    # Get source plan
    source_result = await db.execute(
        select(ForecastPlan).where(
            ForecastPlan.org_id == org_id,
            ForecastPlan.billing_period_id == source_period.id,
        )
    )
    source_plan = source_result.scalar_one_or_none()
    if source_plan is None or not source_plan.items:
        raise ValidationError("Source period has no plan to copy")

    # Get or create target plan (race-safe)
    target_plan = await _get_or_create_plan_row(db, org_id, target_period.id)
    await db.refresh(target_plan, ["billing_period", "items"])
    _require_draft(target_plan)

    existing_keys: set[tuple[int, str]] = set()
    if target_plan.items:
        existing_keys = {(i.category_id, i.type.value) for i in target_plan.items}

    for src_item in source_plan.items:
        key = (src_item.category_id, src_item.type.value)
        if key in existing_keys:
            continue
        new_item = ForecastPlanItem(
            plan_id=target_plan.id,
            org_id=org_id,
            category_id=src_item.category_id,
            type=src_item.type,
            planned_amount=src_item.planned_amount,
            source=src_item.source,
        )
        db.add(new_item)

    await db.commit()
    await db.refresh(target_plan, ["billing_period", "items"])
    return await _build_response(db, org_id, target_plan)


# ── Internal ─────────────────────────────────────────────────────────────────

async def _get_plan(db: AsyncSession, org_id: int, plan_id: int) -> ForecastPlan:
    result = await db.execute(
        select(ForecastPlan).where(
            ForecastPlan.id == plan_id,
            ForecastPlan.org_id == org_id,
        )
    )
    plan = result.scalar_one_or_none()
    if plan is None:
        raise NotFoundError("Forecast plan")
    return plan
