"""Cash-flow Sankey builder.

Produces income-hub Sankey links from the org's reportable transactions:

    income_category → Income → spending_category
                             ↘ Savings  (when income > expense)

Transfer pairs (``linked_transaction_id IS NOT NULL``), manual balance
adjustments (``is_manual_adjustment``), and SKIPPED/REJECTED reconciliation
rows are excluded via ``reportable_transaction_filter()`` — the same predicate
used by budgets, forecasts, and every other aggregate surface.

Why NOT ``execute_query`` / ``ReportsQuery`` ASTs:
    ``execute_query`` in ``reports_query_service`` does NOT apply
    ``reportable_transaction_filter``.  It only scopes by ``org_id`` and
    honours the caller's explicit filters.  Building on top of it would
    silently include transfer legs and manual adjustments in the income and
    expense totals, double-counting cash flow.  The sankey builder therefore
    compiles its own SQLAlchemy Core selects and applies the reportable
    predicate explicitly, inheriting org-scoping, user-supplied date/account
    filters (passed through ``SankeyQuery.filters``), and cash-basis date
    bucketing from the same primitives used by the compiler.
"""
from __future__ import annotations

import time

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category import Category
from app.models.transaction import Transaction, TransactionType
from app.schemas.reports_query import (
    Filter,
    FilterField,
    QueryMeta,
    SankeyLink,
    SankeyQuery,
    SankeyResponse,
)
from app.services.reports_query_service import _apply_scalar_filter, _apply_tag_filter
from app.services.transaction_filters import reportable_transaction_filter


async def build_sankey(
    db: AsyncSession,
    *,
    org_id: int,
    query: SankeyQuery,
) -> SankeyResponse:
    """Build the income-hub Sankey for *org_id*.

    Args:
        db: AsyncSession bound to the caller's request.
        org_id: Injected from ``current_user.org_id``; never from the wire.
        query: Validated ``SankeyQuery`` from the request body.

    Returns:
        ``SankeyResponse`` with links and meta. Empty ``links=[]`` when
        ``income_total == 0`` (the frontend renders an empty state).
    """
    started = time.perf_counter()

    # ── Income aggregation ──────────────────────────────────────────
    # SUM(amount) grouped by category name for all reportable income rows.
    income_cat_expr = Category.name.label("category")
    income_stmt = (
        select(income_cat_expr, func.coalesce(func.sum(Transaction.amount), 0).label("value"))
        .select_from(Transaction)
        .join(Category, Category.id == Transaction.category_id)
        .where(
            Transaction.org_id == org_id,
            Transaction.type == TransactionType.INCOME,
            reportable_transaction_filter(),
        )
    )
    income_stmt = _apply_user_filters(income_stmt, query.filters, org_id)
    income_stmt = income_stmt.group_by(Category.name)

    income_rows = (await db.execute(income_stmt)).mappings().all()

    # ── Expense aggregation ─────────────────────────────────────────
    # Spending side groups by category or category_master per the query.
    if query.spending_granularity == "category_master":
        from sqlalchemy.orm import aliased

        parent = aliased(Category, name="category_master")
        spending_label = func.coalesce(parent.name, Category.name).label("category")
        expense_stmt = (
            select(spending_label, func.coalesce(func.sum(Transaction.amount), 0).label("value"))
            .select_from(Transaction)
            .join(Category, Category.id == Transaction.category_id)
            .outerjoin(parent, parent.id == Category.parent_id)
            .where(
                Transaction.org_id == org_id,
                Transaction.type == TransactionType.EXPENSE,
                reportable_transaction_filter(),
            )
        )
        expense_stmt = _apply_user_filters(expense_stmt, query.filters, org_id)
        expense_stmt = expense_stmt.group_by(func.coalesce(parent.name, Category.name))
    else:
        expense_cat_expr = Category.name.label("category")
        expense_stmt = (
            select(expense_cat_expr, func.coalesce(func.sum(Transaction.amount), 0).label("value"))
            .select_from(Transaction)
            .join(Category, Category.id == Transaction.category_id)
            .where(
                Transaction.org_id == org_id,
                Transaction.type == TransactionType.EXPENSE,
                reportable_transaction_filter(),
            )
        )
        expense_stmt = _apply_user_filters(expense_stmt, query.filters, org_id)
        expense_stmt = expense_stmt.group_by(Category.name)

    expense_rows = (await db.execute(expense_stmt)).mappings().all()

    elapsed_ms = int((time.perf_counter() - started) * 1000)

    # ── Guard: no income → empty result ────────────────────────────
    income_pairs = [(r["category"], _to_float(r["value"])) for r in income_rows if _to_float(r["value"]) > 0]
    expense_pairs = [(r["category"], _to_float(r["value"])) for r in expense_rows if _to_float(r["value"]) > 0]

    income_total = sum(v for _, v in income_pairs)
    if income_total == 0:
        meta = QueryMeta(
            row_count=0,
            truncated=False,
            query_ms=elapsed_ms,
        )
        return SankeyResponse(links=[], meta=meta)

    expense_total = sum(v for _, v in expense_pairs)

    # Capture pre-fold counts for row_count semantics (reflects the number
    # of aggregated categories before top_n folding collapses the tail).
    pre_fold_income_count = len(income_pairs)
    pre_fold_expense_count = len(expense_pairs)

    # ── Apply top_n folding on the expense side ──────────────────────
    if query.top_n is not None and len(expense_pairs) > query.top_n:
        # Sort by value descending; keep top_n, fold remainder into "Other".
        expense_pairs_sorted = sorted(expense_pairs, key=lambda x: x[1], reverse=True)
        top = expense_pairs_sorted[: query.top_n]
        tail_total = sum(v for _, v in expense_pairs_sorted[query.top_n :])
        expense_pairs = top
        if tail_total > 0:
            expense_pairs = list(expense_pairs) + [("Other", tail_total)]

    # ── Build links ─────────────────────────────────────────────────
    links: list[SankeyLink] = []

    # Income category → "Income" hub
    for cat, value in income_pairs:
        source = cat or "Uncategorised"
        if source != "Income":  # guard self-loop
            links.append(SankeyLink(source=source, target="Income", value=value))

    # "Income" hub → spending category
    for cat, value in expense_pairs:
        target = cat or "Uncategorised"
        if target != "Income":  # guard self-loop
            links.append(SankeyLink(source="Income", target=target, value=value))

    # "Income" → "Savings" when income exceeds expense
    if income_total > expense_total:
        savings = income_total - expense_total
        links.append(SankeyLink(source="Income", target="Savings", value=savings))

    meta = QueryMeta(
        row_count=pre_fold_income_count + pre_fold_expense_count,
        truncated=False,
        query_ms=elapsed_ms,
    )
    return SankeyResponse(links=links, meta=meta)


# ── Internal helpers ─────────────────────────────────────────────────


def _to_float(value) -> float:
    """Coerce Decimal or numeric DB result to float."""
    if value is None:
        return 0.0
    if hasattr(value, "as_tuple"):  # Decimal
        try:
            return float(value)
        except Exception:
            return 0.0
    return float(value)


_SANKEY_SUPPORTED_FILTER_FIELDS = {
    FilterField.DATE,
    FilterField.AMOUNT,
    FilterField.CATEGORY_ID,
    FilterField.ACCOUNT_ID,
    FilterField.STATUS,
    FilterField.TAG_NAME,
    # TXN_TYPE is excluded: the builder locks it; handled separately below.
}


def _apply_user_filters(stmt, filters: list[Filter], org_id: int):
    """Apply the caller-supplied SankeyQuery filters to *stmt*.

    Tag filters are routed to ``_apply_tag_filter``; all others to
    ``_apply_scalar_filter``.  The ``txn_type`` filter is silently
    skipped here: the sankey builder already adds its own ``type =
    income / expense`` predicate, so a caller-supplied txn_type filter
    would at best be a no-op and at worst conflict.

    Raises:
        ValueError: when a filter field is not in the Sankey-supported
            whitelist (e.g. ``account_type``, ``currency``, ``balance``,
            ``account_active``, ``frequency``, ``recurring_active``).
            The router maps this to HTTP 422.
    """
    for f in filters:
        if f.field is FilterField.TXN_TYPE:
            # Skip — the builder locks txn_type itself.
            continue
        if f.field not in _SANKEY_SUPPORTED_FILTER_FIELDS:
            supported = sorted(ff.value for ff in _SANKEY_SUPPORTED_FILTER_FIELDS)
            raise ValueError(
                f"Filter field {f.field.value!r} is not supported on the Sankey endpoint. "
                f"Supported fields: {supported}"
            )
        if f.field is FilterField.TAG_NAME:
            stmt = _apply_tag_filter(stmt, f, org_id)
        else:
            stmt = _apply_scalar_filter(stmt, f)
    return stmt
