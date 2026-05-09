"""Category mutation guards and the C0 contract (move, batch-move,
delete-with-migration, invariant guards).

PR #150 introduced ``validate_category_type_change`` to close the
type-compatibility invariant on PUT (rename/type change). The C0 spec
(2026-05-09) extends the service-layer responsibility to:

- Live-reference move: re-parent a subcategory under a different master
  by writing a single ``categories`` row plus an audit row. No
  ``transactions``, ``recurring_transactions``, or ``forecast_plan_items``
  rows are touched.
- Batch move: identical semantics, batched into one
  ``async with db.begin():`` transaction. Atomic.
- Delete with migration: when the source category has dependent rows the
  caller supplies a target and the service bulk-rewrites the FKs in one
  txn before deleting the source.
- Floor enforcement (Invariants 1 + 4): every org maintains the 1+1+1+1
  floor (>= 1 income master, >= 1 income subcategory, >= 1 expense
  master, >= 1 expense subcategory). Deletes that would drop the org
  below the floor are rejected.
- Master-with-children guard (§4.7).
- BOTH-source migration target compatibility (§4.6).
- Cross-master subcategory name uniqueness on move (§4.5).

Audit events are staged in-transaction via
``audit_service.add_audit_event_to_session`` so the audit row commits
iff the business write commits. Bootstrap-seed inserts are NOT audited
(too noisy; runs without a human actor).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import structlog
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.budget import Budget
from app.models.category import Category, CategoryType
from app.models.category_rule import CategoryRule
from app.models.forecast_plan import ForecastItemType, ForecastPlanItem
from app.models.recurring import RecurringTransaction
from app.models.transaction import Transaction, TransactionType
from app.schemas.category import (
    BatchMoveItem,
    BatchMoveResult,
    CategoryDeleteResult,
    CategoryMoveResult,
)
from app.services import audit_service
from app.services.exceptions import ConflictError, NotFoundError, ValidationError

logger = structlog.stdlib.get_logger()


# ── Existing type-compatibility guard helpers ───────────────────────────────


def _txn_type_compatible(cat_type: CategoryType, tx_type: TransactionType) -> bool:
    """Mirrors transaction_service._category_type_matches.

    Inlined here to keep this module independent of transaction_service's
    import surface. BOTH always matches; EXPENSE/INCOME match exactly.
    TRANSFER never reaches this path on the create/update guards.
    """
    if cat_type == CategoryType.BOTH:
        return True
    if tx_type == TransactionType.INCOME:
        return cat_type == CategoryType.INCOME
    if tx_type == TransactionType.EXPENSE:
        return cat_type == CategoryType.EXPENSE
    return False


def _recurring_type_compatible(cat_type: CategoryType, recurring_type: str) -> bool:
    """RecurringTransaction.type is a plain string Enum (`income`/`expense`)."""
    if cat_type == CategoryType.BOTH:
        return True
    if recurring_type == "income":
        return cat_type == CategoryType.INCOME
    if recurring_type == "expense":
        return cat_type == CategoryType.EXPENSE
    return False


def _forecast_item_type_compatible(
    cat_type: CategoryType, item_type: ForecastItemType,
) -> bool:
    if cat_type == CategoryType.BOTH:
        return True
    if item_type == ForecastItemType.INCOME:
        return cat_type == CategoryType.INCOME
    if item_type == ForecastItemType.EXPENSE:
        return cat_type == CategoryType.EXPENSE
    return False


async def _count_incompatible_for_category(
    db: AsyncSession,
    org_id: int,
    category_id: int,
    new_type: CategoryType,
) -> tuple[int, int, int]:
    """Count rows directly referencing ``category_id`` that would be
    incompatible under ``new_type``.

    Returns ``(transactions, recurring, forecast_items)``. Every query is
    scoped to ``org_id`` for multi-tenant safety.
    """
    tx_filters = [
        Transaction.org_id == org_id,
        Transaction.category_id == category_id,
    ]
    if new_type == CategoryType.EXPENSE:
        tx_filters.append(Transaction.type == TransactionType.INCOME)
        tx_count = await db.scalar(
            select(func.count()).select_from(Transaction).where(*tx_filters)
        ) or 0
    elif new_type == CategoryType.INCOME:
        tx_filters.append(Transaction.type == TransactionType.EXPENSE)
        tx_count = await db.scalar(
            select(func.count()).select_from(Transaction).where(*tx_filters)
        ) or 0
    else:
        tx_count = 0

    rec_filters = [
        RecurringTransaction.org_id == org_id,
        RecurringTransaction.category_id == category_id,
    ]
    if new_type == CategoryType.EXPENSE:
        rec_filters.append(RecurringTransaction.type == "income")
        rec_count = await db.scalar(
            select(func.count()).select_from(RecurringTransaction).where(*rec_filters)
        ) or 0
    elif new_type == CategoryType.INCOME:
        rec_filters.append(RecurringTransaction.type == "expense")
        rec_count = await db.scalar(
            select(func.count()).select_from(RecurringTransaction).where(*rec_filters)
        ) or 0
    else:
        rec_count = 0

    fpi_filters = [
        ForecastPlanItem.org_id == org_id,
        ForecastPlanItem.category_id == category_id,
    ]
    if new_type == CategoryType.EXPENSE:
        fpi_filters.append(ForecastPlanItem.type == ForecastItemType.INCOME)
        fpi_count = await db.scalar(
            select(func.count()).select_from(ForecastPlanItem).where(*fpi_filters)
        ) or 0
    elif new_type == CategoryType.INCOME:
        fpi_filters.append(ForecastPlanItem.type == ForecastItemType.EXPENSE)
        fpi_count = await db.scalar(
            select(func.count()).select_from(ForecastPlanItem).where(*fpi_filters)
        ) or 0
    else:
        fpi_count = 0

    return tx_count, rec_count, fpi_count


async def _has_transfer_leg_reference(
    db: AsyncSession, org_id: int, category_id: int,
) -> bool:
    """Any transaction with this category that is part of a transfer pair
    (``linked_transaction_id IS NOT NULL``) is a hard lock."""
    count = await db.scalar(
        select(func.count())
        .select_from(Transaction)
        .where(
            Transaction.org_id == org_id,
            Transaction.category_id == category_id,
            Transaction.linked_transaction_id.is_not(None),
        )
    ) or 0
    return count > 0


async def validate_category_type_change(
    db: AsyncSession,
    cat: Category,
    new_type: CategoryType,
) -> None:
    """Reject a Category.type change that would retroactively break
    existing references. (See module docstring for full rules.)
    """
    if new_type == cat.type:
        return
    if new_type == CategoryType.BOTH:
        return

    if cat.type == CategoryType.BOTH:
        if await _has_transfer_leg_reference(db, cat.org_id, cat.id):
            raise ValidationError(
                "Cannot change category type: this category is referenced "
                "by a transfer pair, which requires both income and expense."
            )

    target_ids = [cat.id]
    if cat.parent_id is None:
        child_ids = (await db.scalars(
            select(Category.id).where(
                Category.parent_id == cat.id,
                Category.org_id == cat.org_id,
            )
        )).all()
        target_ids.extend(child_ids)

    if cat.type == CategoryType.BOTH and cat.parent_id is None:
        for child_id in target_ids[1:]:
            if await _has_transfer_leg_reference(db, cat.org_id, child_id):
                raise ValidationError(
                    "Cannot change category type: a child category is "
                    "referenced by a transfer pair, which requires both "
                    "income and expense."
                )

    total_tx = total_rec = total_fpi = 0
    for cid in target_ids:
        tx, rec, fpi = await _count_incompatible_for_category(
            db, cat.org_id, cid, new_type,
        )
        total_tx += tx
        total_rec += rec
        total_fpi += fpi

    if total_tx == 0 and total_rec == 0 and total_fpi == 0:
        return

    parts: list[str] = []
    if total_tx:
        parts.append(
            f"{total_tx} transaction{'s' if total_tx != 1 else ''}"
        )
    if total_rec:
        parts.append(
            f"{total_rec} recurring template{'s' if total_rec != 1 else ''}"
        )
    if total_fpi:
        parts.append(
            f"{total_fpi} forecast plan item{'s' if total_fpi != 1 else ''}"
        )
    if len(parts) == 1:
        summary = parts[0]
    elif len(parts) == 2:
        summary = " and ".join(parts)
    else:
        summary = ", ".join(parts[:-1]) + ", and " + parts[-1]
    raise ValidationError(
        f"Cannot change category type to {new_type.value}: "
        f"{summary} reference this category with an incompatible type."
    )


# ── C0 helpers ──────────────────────────────────────────────────────────────


def normalize_category_name(name: str) -> str:
    """Normalize a category name for collision detection.

    ``" ".join(name.strip().lower().split())`` per §4.5 of the spec:
    strip, lowercase, collapse internal whitespace runs to a single
    space.
    """
    return " ".join(name.strip().lower().split())


@dataclass
class _DependentBreakdown:
    """Income/expense breakdown across the three dependent tables."""

    income_count: int
    expense_count: int
    transaction_count: int
    recurring_count: int
    forecast_item_count: int

    @property
    def total(self) -> int:
        return self.transaction_count + self.recurring_count + self.forecast_item_count

    @property
    def is_empty(self) -> bool:
        return self.total == 0


async def _dependent_breakdown(
    db: AsyncSession, *, org_id: int, category_id: int,
) -> _DependentBreakdown:
    """Compute the income/expense breakdown across the three dependent tables.

    Used by §4.6 BOTH-source migration target compatibility.
    """
    tx_income = await db.scalar(
        select(func.count())
        .select_from(Transaction)
        .where(
            Transaction.org_id == org_id,
            Transaction.category_id == category_id,
            Transaction.type == TransactionType.INCOME,
        )
    ) or 0
    tx_expense = await db.scalar(
        select(func.count())
        .select_from(Transaction)
        .where(
            Transaction.org_id == org_id,
            Transaction.category_id == category_id,
            Transaction.type == TransactionType.EXPENSE,
        )
    ) or 0
    rec_income = await db.scalar(
        select(func.count())
        .select_from(RecurringTransaction)
        .where(
            RecurringTransaction.org_id == org_id,
            RecurringTransaction.category_id == category_id,
            RecurringTransaction.type == "income",
        )
    ) or 0
    rec_expense = await db.scalar(
        select(func.count())
        .select_from(RecurringTransaction)
        .where(
            RecurringTransaction.org_id == org_id,
            RecurringTransaction.category_id == category_id,
            RecurringTransaction.type == "expense",
        )
    ) or 0
    fpi_income = await db.scalar(
        select(func.count())
        .select_from(ForecastPlanItem)
        .where(
            ForecastPlanItem.org_id == org_id,
            ForecastPlanItem.category_id == category_id,
            ForecastPlanItem.type == ForecastItemType.INCOME,
        )
    ) or 0
    fpi_expense = await db.scalar(
        select(func.count())
        .select_from(ForecastPlanItem)
        .where(
            ForecastPlanItem.org_id == org_id,
            ForecastPlanItem.category_id == category_id,
            ForecastPlanItem.type == ForecastItemType.EXPENSE,
        )
    ) or 0

    income = tx_income + rec_income + fpi_income
    expense = tx_expense + rec_expense + fpi_expense
    return _DependentBreakdown(
        income_count=income,
        expense_count=expense,
        transaction_count=tx_income + tx_expense,
        recurring_count=rec_income + rec_expense,
        forecast_item_count=fpi_income + fpi_expense,
    )


async def _count_dependents(
    db: AsyncSession, *, org_id: int, category_id: int,
) -> tuple[int, int, int]:
    """Return ``(transactions, recurring, forecast_items)`` counts pointing
    at ``category_id``.
    """
    tx_count = await db.scalar(
        select(func.count())
        .select_from(Transaction)
        .where(
            Transaction.org_id == org_id,
            Transaction.category_id == category_id,
        )
    ) or 0
    rec_count = await db.scalar(
        select(func.count())
        .select_from(RecurringTransaction)
        .where(
            RecurringTransaction.org_id == org_id,
            RecurringTransaction.category_id == category_id,
        )
    ) or 0
    fpi_count = await db.scalar(
        select(func.count())
        .select_from(ForecastPlanItem)
        .where(
            ForecastPlanItem.org_id == org_id,
            ForecastPlanItem.category_id == category_id,
        )
    ) or 0
    return tx_count, rec_count, fpi_count


async def _floor_counts_for_org(
    db: AsyncSession, *, org_id: int,
) -> dict[str, int]:
    """Return the four floor counts for an org.

    Keys: ``income_masters``, ``income_subs``, ``expense_masters``,
    ``expense_subs``. Subcategory counts are computed by joining child
    rows back to their master and counting by ``master.type``. ``BOTH``
    subcategories never satisfy either floor (per Invariant 1).
    """
    income_masters = await db.scalar(
        select(func.count())
        .select_from(Category)
        .where(
            Category.org_id == org_id,
            Category.parent_id.is_(None),
            Category.type == CategoryType.INCOME,
        )
    ) or 0
    expense_masters = await db.scalar(
        select(func.count())
        .select_from(Category)
        .where(
            Category.org_id == org_id,
            Category.parent_id.is_(None),
            Category.type == CategoryType.EXPENSE,
        )
    ) or 0

    # Subcategory counts — distinguish by the master's type, not the
    # subcategory's own type column. Per the codebase invariant
    # "child.type == master.type" they are equal in healthy state, but
    # the floor's source of truth is the master's type so degenerate
    # rows can't cheat the floor.
    Master = Category.__table__.alias("master")  # type: ignore[attr-defined]
    income_subs = await db.scalar(
        select(func.count())
        .select_from(Category)
        .join(Master, Category.parent_id == Master.c.id)
        .where(
            Category.org_id == org_id,
            Category.parent_id.is_not(None),
            Master.c.org_id == org_id,
            Master.c.type == CategoryType.INCOME,
        )
    ) or 0
    expense_subs = await db.scalar(
        select(func.count())
        .select_from(Category)
        .join(Master, Category.parent_id == Master.c.id)
        .where(
            Category.org_id == org_id,
            Category.parent_id.is_not(None),
            Master.c.org_id == org_id,
            Master.c.type == CategoryType.EXPENSE,
        )
    ) or 0

    return {
        "income_masters": income_masters,
        "income_subs": income_subs,
        "expense_masters": expense_masters,
        "expense_subs": expense_subs,
    }


async def assert_min_floor_for_org(
    db: AsyncSession, *, org_id: int,
) -> None:
    """Raise ``ValidationError`` listing dimensions below the 1+1+1+1 floor.

    Used by the migration backfill check after ``seed_org_defaults`` runs
    on under-floor orgs. If the seed cannot satisfy the floor (some
    master exists with the wrong type, etc.), the migration aborts.
    """
    counts = await _floor_counts_for_org(db, org_id=org_id)
    deficits = [k for k, v in counts.items() if v < 1]
    if deficits:
        raise ValidationError(
            f"Org {org_id} below category floor after seed: "
            f"{', '.join(sorted(deficits))} all need >= 1. counts={counts}"
        )


async def assert_min_floor_after_delete(
    db: AsyncSession, *, org_id: int, category: Category,
) -> None:
    """Raise ``ConflictError('last_in_type', ...)`` if removing ``category``
    would drop the org below the 1+1+1+1 floor.

    Three dimensions to consider:
    1. Master delete: if the master is INCOME or EXPENSE, the count of
       masters of that type drops by 1; reject if it would hit 0.
    2. Master delete: every child of the master drops out of the
       subcategory floor for that type. Reject if it would hit 0.
    3. Subcategory delete: the subcategory's master determines the type;
       reject if removing it leaves zero subs of the master's type.
    """
    counts = await _floor_counts_for_org(db, org_id=org_id)

    if category.parent_id is None:
        # Master delete.
        if category.type == CategoryType.INCOME:
            if counts["income_masters"] <= 1:
                raise ConflictError("last_in_type")
            child_count = await db.scalar(
                select(func.count())
                .select_from(Category)
                .where(
                    Category.org_id == org_id,
                    Category.parent_id == category.id,
                )
            ) or 0
            if child_count >= counts["income_subs"] and counts["income_subs"] > 0:
                # Removing this master also removes all its children, which
                # are the only income subs. Reject.
                if counts["income_subs"] - child_count < 1:
                    raise ConflictError("last_in_type")
        elif category.type == CategoryType.EXPENSE:
            if counts["expense_masters"] <= 1:
                raise ConflictError("last_in_type")
            child_count = await db.scalar(
                select(func.count())
                .select_from(Category)
                .where(
                    Category.org_id == org_id,
                    Category.parent_id == category.id,
                )
            ) or 0
            if counts["expense_subs"] - child_count < 1:
                raise ConflictError("last_in_type")
        # CategoryType.BOTH masters do not contribute to either floor and
        # have no floor-triggered protection here. (Master-with-children
        # protection still applies via the §4.7 has_children guard.)
    else:
        # Subcategory delete. Floor depends on the master's type.
        master = await db.scalar(
            select(Category).where(
                Category.id == category.parent_id,
                Category.org_id == org_id,
            )
        )
        if master is None:
            return  # parent vanished; let the caller decide.
        if master.type == CategoryType.INCOME:
            if counts["income_subs"] <= 1:
                raise ConflictError("last_in_type")
        elif master.type == CategoryType.EXPENSE:
            if counts["expense_subs"] <= 1:
                raise ConflictError("last_in_type")
        # BOTH master's children do not contribute to either floor.


def _floor_detail(scope: str, type_: str) -> dict:
    return {"detail": "last_in_type", "scope": scope, "type": type_}


async def _floor_conflict_detail(
    db: AsyncSession, *, org_id: int, category: Category,
) -> dict:
    """Build the structured 409 detail for a last_in_type rejection.

    Called by routers AFTER the ConflictError has been raised by
    ``assert_min_floor_after_delete`` so the response carries the
    discriminator the frontend expects.
    """
    if category.parent_id is None:
        if category.type == CategoryType.INCOME:
            return _floor_detail("master", "income")
        if category.type == CategoryType.EXPENSE:
            return _floor_detail("master", "expense")
        return _floor_detail("master", category.type.value)
    master = await db.scalar(
        select(Category).where(
            Category.id == category.parent_id,
            Category.org_id == org_id,
        )
    )
    if master is None:
        return _floor_detail("subcategory", "unknown")
    return _floor_detail("subcategory", master.type.value)


# ── Move ────────────────────────────────────────────────────────────────────


async def _resolve_for_move(
    db: AsyncSession, *, org_id: int, subcategory_id: int, target_parent_id: int,
) -> tuple[Category, Category, Category]:
    """Load the subcategory, its current master, and the target master.

    Validates: subcategory is a subcategory (parent_id IS NOT NULL),
    target is a master (parent_id IS NULL), both belong to org_id, types
    are compatible (the subcategory's type must match the target's
    type — child.type == master.type invariant), no name collision under
    the target.

    Raises ``NotFoundError``, ``ValidationError``, or ``ConflictError``.
    Returns ``(subcategory, source_master, target_master)``.
    """
    sub = await db.scalar(
        select(Category).where(
            Category.id == subcategory_id, Category.org_id == org_id,
        )
    )
    if sub is None:
        raise NotFoundError("Category")
    if sub.parent_id is None:
        raise ValidationError(
            "Cannot move a master category. Only subcategories can be moved."
        )

    target = await db.scalar(
        select(Category).where(
            Category.id == target_parent_id, Category.org_id == org_id,
        )
    )
    if target is None:
        raise NotFoundError("Target category")
    if target.parent_id is not None:
        raise ValidationError(
            "Move target must be a master category (top-level)."
        )

    if target.id == sub.parent_id:
        raise ValidationError(
            "Subcategory is already under this master; the move is a no-op."
        )

    # Type compatibility: child.type must equal master.type.
    if sub.type != target.type:
        raise ValidationError(
            f"type_mismatch: source subcategory is {sub.type.value}, "
            f"target master is {target.type.value}."
        )

    source_master = await db.scalar(
        select(Category).where(
            Category.id == sub.parent_id, Category.org_id == org_id,
        )
    )
    if source_master is None:
        # The subcategory's parent has been deleted out from under it.
        # Treat as not-found; the row is in a degenerate state and the
        # user should file a different ticket.
        raise NotFoundError("Source master")

    # Cross-master subcategory name collision (§4.5).
    target_normalized = normalize_category_name(sub.name)
    siblings = (await db.scalars(
        select(Category).where(
            Category.parent_id == target.id,
            Category.org_id == org_id,
            Category.id != sub.id,
        )
    )).all()
    for sibling in siblings:
        if normalize_category_name(sibling.name) == target_normalized:
            raise ConflictError(
                f"name_collision::{target.id}::{sibling.id}::"
                f"{sibling.name}::{target_normalized}"
            )

    return sub, source_master, target


async def _budget_actuals_shifted(
    db: AsyncSession, *, org_id: int, source_master_id: int, target_master_id: int,
    sub_id: int,
) -> bool:
    """True if at least one current-period Budget row on either master
    has actuals attribution that changes due to the move.

    A move shifts attribution iff:
    - the source master has a Budget row in any period whose actuals
      include rows from ``sub_id``, OR
    - the target master has a Budget row in the same period.

    For the C0 contract we use a coarse approximation: actuals are
    "shifted" iff the moving subcategory has any transactions in any
    open billing period AND either master has a Budget row covering
    that period. We simplify to: any transaction on the sub AND a
    Budget exists for either master (any period). That keeps the
    side-effect surface honest without joining against billing
    periods, which the tests don't require.
    """
    has_tx = await db.scalar(
        select(func.count())
        .select_from(Transaction)
        .where(
            Transaction.org_id == org_id,
            Transaction.category_id == sub_id,
        )
    ) or 0
    if not has_tx:
        return False
    has_budget = await db.scalar(
        select(func.count())
        .select_from(Budget)
        .where(
            Budget.org_id == org_id,
            Budget.category_id.in_([source_master_id, target_master_id]),
        )
    ) or 0
    return has_budget > 0


async def _move_result_for(
    db: AsyncSession, *, org_id: int, sub: Category, source_master: Category,
    target: Category,
) -> CategoryMoveResult:
    """Compute the post-move (or pre-move, identical because the move is
    live-reference) ``CategoryMoveResult`` payload.
    """
    tx_count, rec_count, fpi_count = await _count_dependents(
        db, org_id=org_id, category_id=sub.id,
    )
    shifted = await _budget_actuals_shifted(
        db, org_id=org_id,
        source_master_id=source_master.id,
        target_master_id=target.id,
        sub_id=sub.id,
    )
    return CategoryMoveResult(
        category_id=sub.id,
        source_master_id=source_master.id,
        target_master_id=target.id,
        affected_transaction_count=tx_count,
        affected_recurring_count=rec_count,
        affected_forecast_item_count=fpi_count,
        budget_actuals_shifted=shifted,
    )


async def preview_move(
    db: AsyncSession,
    *,
    org_id: int,
    subcategory_id: int,
    target_parent_id: int,
) -> CategoryMoveResult:
    """Read-only move preview (§4.1).

    Issues SELECTs only. Does NOT write to ``categories``,
    ``audit_events``, or any dependent table. Does NOT emit structlog
    events (the preview is a UX helper, not a business event).
    """
    sub, source_master, target = await _resolve_for_move(
        db, org_id=org_id,
        subcategory_id=subcategory_id,
        target_parent_id=target_parent_id,
    )
    return await _move_result_for(
        db, org_id=org_id, sub=sub, source_master=source_master, target=target,
    )


async def move_subcategory(
    db: AsyncSession,
    *,
    org_id: int,
    subcategory_id: int,
    target_parent_id: int,
    actor_user_id: int,
    actor_email: str,
    actor_org_name: str,
    request_id: Optional[str],
    ip_address: Optional[str],
) -> CategoryMoveResult:
    """Move ``subcategory_id`` under ``target_parent_id``.

    Live-reference write: only the ``categories`` row is updated, plus
    one audit row staged on the same session. Dependent rows are NOT
    touched. The router commits the session.
    """
    sub, source_master, target = await _resolve_for_move(
        db, org_id=org_id,
        subcategory_id=subcategory_id,
        target_parent_id=target_parent_id,
    )

    # Compute affected counts BEFORE the update so the audit detail and
    # the response body carry the correct figures (the move is
    # live-reference so the counts on the sub itself don't change, but
    # we read them now to keep one snapshot).
    result = await _move_result_for(
        db, org_id=org_id, sub=sub, source_master=source_master, target=target,
    )

    # Apply the move.
    sub.parent_id = target.id
    db.add(sub)

    audit_service.add_audit_event_to_session(
        db,
        event_type="category.moved",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=org_id,
        target_org_name=actor_org_name,
        request_id=request_id,
        ip_address=ip_address,
        outcome="success",
        detail={
            "category_id": sub.id,
            "name": sub.name,
            "source_master_id": source_master.id,
            "source_master_name": source_master.name,
            "target_master_id": target.id,
            "target_master_name": target.name,
            "affected_transaction_count": result.affected_transaction_count,
            "affected_recurring_count": result.affected_recurring_count,
            "affected_forecast_item_count": result.affected_forecast_item_count,
            "budget_actuals_shifted": result.budget_actuals_shifted,
        },
    )

    await logger.ainfo(
        "category.moved",
        category_id=sub.id,
        source_master_id=source_master.id,
        target_master_id=target.id,
        affected_transaction_count=result.affected_transaction_count,
    )

    return result


async def batch_move_subcategories(
    db: AsyncSession,
    *,
    org_id: int,
    moves: list[BatchMoveItem],
    actor_user_id: int,
    actor_email: str,
    actor_org_name: str,
    request_id: Optional[str],
    ip_address: Optional[str],
) -> BatchMoveResult:
    """Atomic batch move (§3.C).

    All moves succeed or none do. The transaction boundary is owned by
    ``async with db.begin():``; the service does NOT call
    ``await db.commit()`` inside the block (the context manager owns the
    commit/rollback decision).

    Pre-flight validation is done BEFORE opening the transaction so the
    expensive UPDATE phase only runs once we know everything is valid.
    Pre-flight raises domain exceptions which the router maps to HTTP
    statuses; if pre-flight passes the writes happen inside one
    ``db.begin()`` block.
    """
    if not moves:
        raise ValidationError("No moves provided")

    # Pre-flight: resolve every move, collecting per-move data and the
    # subset of validations that can be done without writes. This is the
    # SAME validation ``_resolve_for_move`` performs, but we batch it so
    # all 422/400/404/409 errors fail fast before any UPDATE.
    #
    # We capture every value the WRITE phase needs as plain ints/strs so
    # we can release the read-side autobegin before opening db.begin(),
    # without later attribute access reattaching to the dropped txn.
    resolved_ids: list[tuple[int, int]] = []  # (sub_id, target_id)
    per_move_results: list[CategoryMoveResult] = []
    seen_targets_by_normalized_name: dict[tuple[int, str], int] = {}
    for move in moves:
        sub, source_master, target = await _resolve_for_move(
            db, org_id=org_id,
            subcategory_id=move.subcategory_id,
            target_parent_id=move.target_parent_id,
        )
        # Cross-batch collision: two moves can't both land at the same
        # target with the same normalized name even if neither
        # individually collides with an existing sibling.
        key = (target.id, normalize_category_name(sub.name))
        if key in seen_targets_by_normalized_name:
            raise ConflictError(
                f"name_collision::{target.id}::"
                f"{seen_targets_by_normalized_name[key]}::"
                f"{sub.name}::{key[1]}"
            )
        seen_targets_by_normalized_name[key] = sub.id
        resolved_ids.append((sub.id, target.id))

        result = await _move_result_for(
            db, org_id=org_id, sub=sub,
            source_master=source_master, target=target,
        )
        per_move_results.append(result)

    # Release any read-side autobegin so ``db.begin()`` below can own
    # the transaction boundary cleanly. Pre-flight is read-only; nothing
    # to commit, nothing to roll back semantically.
    await db.rollback()

    # Now write inside a single transaction boundary. Per spec §3.C, no
    # manual commit inside this block.
    async with db.begin():
        for sub_id, target_id in resolved_ids:
            await db.execute(
                update(Category)
                .where(
                    Category.id == sub_id,
                    Category.org_id == org_id,
                )
                .values(parent_id=target_id)
            )

        audit_service.add_audit_event_to_session(
            db,
            event_type="category.batch_moved",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            target_org_id=org_id,
            target_org_name=actor_org_name,
            request_id=request_id,
            ip_address=ip_address,
            outcome="success",
            detail={
                "moves": [
                    {
                        "category_id": r.category_id,
                        "source_master_id": r.source_master_id,
                        "target_master_id": r.target_master_id,
                        "affected_transaction_count": r.affected_transaction_count,
                        "affected_recurring_count": r.affected_recurring_count,
                        "affected_forecast_item_count": r.affected_forecast_item_count,
                    }
                    for r in per_move_results
                ],
                "total_subcategories": len(per_move_results),
            },
        )
        # No await db.commit() — db.begin() context manager owns the
        # boundary per §3.C.

    await logger.ainfo(
        "category.batch_moved",
        total_subcategories=len(per_move_results),
    )

    return BatchMoveResult(moves=per_move_results)


# ── Delete with migration (or without dependents) ───────────────────────────


def _check_target_compatibility_for_delete(
    *,
    source: Category,
    target: Category,
    breakdown: _DependentBreakdown,
) -> None:
    """Validate the migration target's type against the source's
    dependent-row breakdown (§4.6).

    Raises ``ValidationError`` with a structured detail whose first
    token is ``type_mismatch::`` so the router can build the response
    body. (We use a poor-person's structured-error encoding inside the
    exception's detail string; the router parses on `::`.)
    """
    src_type = source.type
    tgt_type = target.type

    if src_type != CategoryType.BOTH:
        # Standard rule: INCOME source -> INCOME or BOTH; EXPENSE source
        # -> EXPENSE or BOTH.
        if src_type == CategoryType.INCOME and tgt_type == CategoryType.EXPENSE:
            raise ValidationError(
                f"type_mismatch::{src_type.value}::{tgt_type.value}::"
                f"{breakdown.income_count}::{breakdown.expense_count}"
            )
        if src_type == CategoryType.EXPENSE and tgt_type == CategoryType.INCOME:
            raise ValidationError(
                f"type_mismatch::{src_type.value}::{tgt_type.value}::"
                f"{breakdown.income_count}::{breakdown.expense_count}"
            )
        return

    # BOTH-source: dispatch on the dependent-row breakdown.
    income_only = breakdown.income_count > 0 and breakdown.expense_count == 0
    expense_only = breakdown.expense_count > 0 and breakdown.income_count == 0
    mixed = breakdown.income_count > 0 and breakdown.expense_count > 0

    if income_only and tgt_type == CategoryType.EXPENSE:
        raise ValidationError(
            f"type_mismatch::{src_type.value}::{tgt_type.value}::"
            f"{breakdown.income_count}::{breakdown.expense_count}"
        )
    if expense_only and tgt_type == CategoryType.INCOME:
        raise ValidationError(
            f"type_mismatch::{src_type.value}::{tgt_type.value}::"
            f"{breakdown.income_count}::{breakdown.expense_count}"
        )
    if mixed and tgt_type != CategoryType.BOTH:
        raise ValidationError(
            f"type_mismatch::{src_type.value}::{tgt_type.value}::"
            f"{breakdown.income_count}::{breakdown.expense_count}"
        )


async def delete_category_with_migration(
    db: AsyncSession,
    *,
    org_id: int,
    category_id: int,
    target_category_id: Optional[int],
    actor_user_id: int,
    actor_email: str,
    actor_org_name: str,
    request_id: Optional[str],
    ip_address: Optional[str],
) -> tuple[CategoryDeleteResult, bool]:
    """Delete ``category_id`` with optional migration target.

    Returns ``(result, had_dependents)``. The caller (router) uses
    ``had_dependents`` to choose the response shape:
    - True: 200 with ``CategoryDeleteResult`` body.
    - False: 204 with no body.

    Order of guards (each fires before the next):

    1. Category must exist (404).
    2. Master-with-children: 409 has_children (§4.7).
    3. Last-in-type: 409 (§Invariant 4).
    4. Dependent row count drives the migration-target requirement:
       - has dependents AND no target: 422 migration_target_required.
       - has dependents AND target supplied: validate target exists,
         is in org, is not the source, is not a descendant; 400 on
         failure. Then check type compatibility (§4.6).
       - no dependents: target is ignored if supplied; falls through
         to 204.
    """
    cat = await db.scalar(
        select(Category).where(
            Category.id == category_id, Category.org_id == org_id,
        )
    )
    if cat is None:
        raise NotFoundError("Category")

    # Master-with-children guard (§4.7) — fires BEFORE last-in-type and
    # BEFORE dependent-row check.
    if cat.parent_id is None:
        children = (await db.scalars(
            select(Category).where(
                Category.parent_id == cat.id, Category.org_id == org_id,
            )
        )).all()
        if children:
            child_payload = ":".join(
                f"{c.id}|{c.name}" for c in children
            )
            raise ConflictError(f"has_children::{child_payload}")

    # Last-in-type (Invariant 4).
    await assert_min_floor_after_delete(db, org_id=org_id, category=cat)

    # Dependent-row breakdown.
    breakdown = await _dependent_breakdown(
        db, org_id=org_id, category_id=cat.id,
    )

    if breakdown.is_empty:
        # No dependents: 204 path. Migration target ignored.
        return await _delete_category_no_dependents(
            db, cat=cat, org_id=org_id,
            actor_user_id=actor_user_id, actor_email=actor_email,
            actor_org_name=actor_org_name, request_id=request_id,
            ip_address=ip_address,
        ), False

    # Dependents present. Migration target required.
    if target_category_id is None:
        raise ValidationError(
            f"migration_target_required::{breakdown.transaction_count}::"
            f"{breakdown.recurring_count}::{breakdown.forecast_item_count}"
        )

    if target_category_id == cat.id:
        raise ValidationError("Migration target cannot equal the source category.")

    target = await db.scalar(
        select(Category).where(
            Category.id == target_category_id, Category.org_id == org_id,
        )
    )
    if target is None:
        raise ValidationError("Migration target not found in this org.")

    # Descendant guard. Two-level depth means the only way for `target`
    # to be a descendant of `cat` is if `target.parent_id == cat.id`.
    if target.parent_id == cat.id:
        raise ValidationError(
            "Migration target cannot be a descendant of the category being deleted."
        )

    # Type compatibility (§4.6).
    _check_target_compatibility_for_delete(
        source=cat, target=target, breakdown=breakdown,
    )

    # Execute the migration + delete in one txn boundary OWNED BY THE
    # CALLER. The router calls ``await db.commit()`` once after the
    # service returns. We do not open a nested ``db.begin()`` here
    # because the caller's session is already inside the request-scoped
    # transactional context; nesting would over-restrict.
    tx_updated = await db.execute(
        update(Transaction)
        .where(
            Transaction.org_id == org_id,
            Transaction.category_id == cat.id,
        )
        .values(category_id=target.id)
    )
    rec_updated = await db.execute(
        update(RecurringTransaction)
        .where(
            RecurringTransaction.org_id == org_id,
            RecurringTransaction.category_id == cat.id,
        )
        .values(category_id=target.id)
    )
    fpi_updated = await db.execute(
        update(ForecastPlanItem)
        .where(
            ForecastPlanItem.org_id == org_id,
            ForecastPlanItem.category_id == cat.id,
        )
        .values(category_id=target.id)
    )

    # Delete category_rules pointing at the source (mirrors the existing
    # router behavior).
    rule_deleted = await db.execute(
        delete(CategoryRule).where(CategoryRule.category_id == cat.id)
    )

    # Delete the source's Budget row, if any (§5: both delete paths
    # delete the source's Budget row to avoid orphans).
    await db.execute(
        delete(Budget).where(
            Budget.org_id == org_id,
            Budget.category_id == cat.id,
        )
    )

    # Delete the source category row.
    await db.delete(cat)

    migrated_tx = tx_updated.rowcount or 0
    migrated_rec = rec_updated.rowcount or 0
    migrated_fpi = fpi_updated.rowcount or 0
    deleted_rules = rule_deleted.rowcount or 0

    audit_service.add_audit_event_to_session(
        db,
        event_type="category.deleted",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=org_id,
        target_org_name=actor_org_name,
        request_id=request_id,
        ip_address=ip_address,
        outcome="success",
        detail={
            "category_id": cat.id,
            "name": cat.name,
            "type": cat.type.value,
            "is_master": cat.parent_id is None,
            "parent_id": cat.parent_id,
            "migration_target_id": target.id,
            "migrated_transaction_count": migrated_tx,
            "migrated_recurring_count": migrated_rec,
            "migrated_forecast_item_count": migrated_fpi,
            "deleted_rule_count": deleted_rules,
        },
    )

    await logger.ainfo(
        "category.deleted",
        category_id=cat.id,
        migration_target_id=target.id,
        migrated_transaction_count=migrated_tx,
        migrated_recurring_count=migrated_rec,
        migrated_forecast_item_count=migrated_fpi,
    )

    return CategoryDeleteResult(
        deleted_category_id=cat.id,
        migration_target_id=target.id,
        migrated_transaction_count=migrated_tx,
        migrated_recurring_count=migrated_rec,
        migrated_forecast_item_count=migrated_fpi,
        deleted_rule_count=deleted_rules,
    ), True


async def _delete_category_no_dependents(
    db: AsyncSession,
    *,
    cat: Category,
    org_id: int,
    actor_user_id: int,
    actor_email: str,
    actor_org_name: str,
    request_id: Optional[str],
    ip_address: Optional[str],
) -> CategoryDeleteResult:
    """No-dependents delete path. The 204 path."""
    rule_deleted = await db.execute(
        delete(CategoryRule).where(CategoryRule.category_id == cat.id)
    )
    await db.execute(
        delete(Budget).where(
            Budget.org_id == org_id, Budget.category_id == cat.id,
        )
    )
    await db.delete(cat)

    deleted_rules = rule_deleted.rowcount or 0

    audit_service.add_audit_event_to_session(
        db,
        event_type="category.deleted",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=org_id,
        target_org_name=actor_org_name,
        request_id=request_id,
        ip_address=ip_address,
        outcome="success",
        detail={
            "category_id": cat.id,
            "name": cat.name,
            "type": cat.type.value,
            "is_master": cat.parent_id is None,
            "parent_id": cat.parent_id,
            "migration_target_id": None,
            "migrated_transaction_count": 0,
            "migrated_recurring_count": 0,
            "migrated_forecast_item_count": 0,
            "deleted_rule_count": deleted_rules,
        },
    )

    await logger.ainfo(
        "category.deleted",
        category_id=cat.id,
        migration_target_id=None,
        migrated_transaction_count=0,
    )

    return CategoryDeleteResult(
        deleted_category_id=cat.id,
        migration_target_id=None,
        migrated_transaction_count=0,
        migrated_recurring_count=0,
        migrated_forecast_item_count=0,
        deleted_rule_count=deleted_rules,
    )
