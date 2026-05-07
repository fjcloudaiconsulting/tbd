"""Category mutation guards.

Closes the third HIGH finding from PR #150 review: PUT /api/v1/categories/{id}
let `cat.type` be reassigned freely, retroactively breaking every
(type, category) compatibility guard added in transaction_service,
recurring_service, and forecast_plan_service.

The single helper exposed here, ``validate_category_type_change``, is called
from the categories router before the type assignment is applied. It runs
COUNT queries scoped to the category's org to detect any existing reference
that would become incompatible under the new type, and raises
``ValidationError`` with an aggregate count summary if any are found.

Master/child semantics
----------------------
The org bootstrap code (org_bootstrap_service.py) seeds children with the
exact same ``CategoryType`` as their master. The transaction-level guard
(``transaction_service.validate_category_for_type``) also rejects a write
whose subcategory's master type doesn't match the transaction type, so the
codebase invariant is "child type == master type".

When the master's type changes we therefore cascade-update every child's
type to the new value as part of the same operation. That cascade is only
safe if every child's existing references are also compatible with the new
type, so we recurse into each child's references during the pre-flight
check and reject if any of them would break.

Transfer leg lockdown
---------------------
A ``CategoryType.BOTH`` category that is referenced by a transfer leg
(``Transaction.linked_transaction_id IS NOT NULL``) cannot move off ``BOTH``
at all. The transfer pair structurally needs the same category on both
legs (one EXPENSE, one INCOME), and ``validate_transfer_category`` (added
in commit b4441d4) refuses to pair on anything other than ``BOTH``.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category import Category, CategoryType
from app.models.forecast_plan import ForecastItemType, ForecastPlanItem
from app.models.recurring import RecurringTransaction
from app.models.transaction import Transaction, TransactionType
from app.services.exceptions import ValidationError


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
    # Transactions: filter by the new type's complementary leg.
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
        # BOTH: every transaction is compatible.
        tx_count = 0

    # Recurring templates (string enum values "income" / "expense").
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

    # Forecast plan items.
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
    (``linked_transaction_id IS NOT NULL``) is a hard lock — see module
    docstring."""
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
    existing references.

    Skipped entirely when ``new_type == cat.type``. ``new_type == BOTH`` is
    always safe (BOTH is compatible with any tx/recurring/forecast type)
    UNLESS ... actually BOTH is the loosest type, so widening is always
    safe and we short-circuit.

    Master categories cascade into their children: every child's references
    are checked against ``new_type`` as well, since the codebase invariant
    is "child type == master type" (see module docstring). The router is
    responsible for cascading the actual ``type`` assignment to the
    children once this guard returns.

    Raises ``ValidationError`` (HTTP 400 via app.main.validation_handler)
    with an aggregate count summary so the admin sees how many rows
    block the change without leaking row IDs or PII.
    """
    if new_type == cat.type:
        return
    if new_type == CategoryType.BOTH:
        # Widening to BOTH is always safe.
        return

    # Transfer-leg lockdown applies when the category itself is currently
    # BOTH. (Children of a BOTH master cannot themselves be BOTH unless the
    # invariant is violated; we still check the master's own references
    # below, and children get their own counts.)
    if cat.type == CategoryType.BOTH:
        if await _has_transfer_leg_reference(db, cat.org_id, cat.id):
            raise ValidationError(
                "Cannot change category type: this category is referenced "
                "by a transfer pair, which requires both income and expense."
            )

    # Build the set of category IDs we need to check: this category + every
    # child (one level — categories are limited to two levels by the create
    # endpoint's validation).
    target_ids = [cat.id]
    if cat.parent_id is None:
        child_ids = (await db.scalars(
            select(Category.id).where(
                Category.parent_id == cat.id,
                Category.org_id == cat.org_id,
            )
        )).all()
        target_ids.extend(child_ids)

    # Children of a BOTH master could themselves carry transfer-leg refs
    # in degenerate seed data; check each.
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
