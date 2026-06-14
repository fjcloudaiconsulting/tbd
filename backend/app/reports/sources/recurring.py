"""Recurring source — amount / count over recurring templates.

Recurring templates are a snapshot table: one row per template, with
``amount`` stored as a column. So this source compiles to a single
``RecurringTransaction JOIN Account JOIN Category`` SELECT — no
transaction reconstruction. ``org_id`` is injected here (HARD
requirement) and scoped on ``recurring_transactions.org_id`` only;
Account and Category are reachable solely through their FKs, so they
need no separate org filter.

The compiler is structurally incapable of emitting SQL for any field
outside its own catalog (date / status / … are silently ignored),
which is what makes the shared-canvas date-bar drop a stray date filter
instead of erroring — the Phase-5 contract.

Note: ``RecurringTransaction.frequency`` is a true ``Frequency`` enum
column (mapped to ``Enum(Frequency)``), so the driver may hand back a
``Frequency`` member rather than its string value. ``RecurringTransaction.type``
is a plain-string ``Enum`` and returns a str. Either way, the row
coercion below normalizes any ``enum.Enum`` dimension value to ``.value``
so grouped JSON keys are always plain strings ("monthly", "expense").
"""
from __future__ import annotations

import enum as _enum
import time
from typing import Any

from sqlalchemy import case, distinct, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.category import Category
from app.models.recurring import RecurringTransaction
from app.reports.sources import register
from app.reports.sources.base import (
    ReportSource, SourceDimension, SourceFilter, SourceMeasure,
    validate_against_catalog,
)
from app.schemas.reports_query import (
    MAX_LIMIT,
    Aggregation,
    Dimension,
    FilterField,
    FilterOp,
    MeasureField,
    ReportsQuery,
)

_DIMENSIONS = [
    SourceDimension("category", "Category", "category"),
    SourceDimension("account", "Account", "account"),
    SourceDimension("currency", "Currency", "currency"),
    SourceDimension("txn_type", "Type", "type"),
    SourceDimension("frequency", "Frequency", "type"),
    SourceDimension("recurring_active", "Status", "boolean"),
]

_MEASURES = [
    SourceMeasure("sum_amount", "Total amount", "sum", "amount", "currency"),
    SourceMeasure("avg_amount", "Average amount", "avg", "amount", "currency"),
    SourceMeasure("count_recurring", "Recurring count", "count", "id", "number"),
]

_FILTERS = [
    SourceFilter("account_id", "Account", ("in",), "account"),
    SourceFilter("category_id", "Category", ("eq", "in"), "category"),
    SourceFilter("currency", "Currency", ("eq", "in"), "currency"),
    SourceFilter("txn_type", "Type", ("eq", "in"), "type"),
    SourceFilter("frequency", "Frequency", ("eq", "in"), "type"),
    SourceFilter("recurring_active", "Status", ("eq",), "boolean"),
    SourceFilter("amount", "Amount", ("between", "gte", "lte"), "number"),
]

# Dimension → (response key, SQL expression). recurring_active normalizes
# the boolean to stable "Active"/"Inactive" strings in the SELECT via a
# CASE so we never depend on the driver returning a Python bool.
_DIM_EXPR = {
    Dimension.CATEGORY: ("category", Category.name),
    Dimension.ACCOUNT: ("account", Account.name),
    Dimension.CURRENCY: ("currency", Account.currency),
    Dimension.TXN_TYPE: ("txn_type", RecurringTransaction.type),
    Dimension.FREQUENCY: ("frequency", RecurringTransaction.frequency),
    Dimension.RECURRING_ACTIVE: (
        "recurring_active",
        case((RecurringTransaction.is_active.is_(True), "Active"), else_="Inactive"),
    ),
}

# Only the source's OWN filter fields are compiled. Anything else
# (date, status, account_type, …) is silently ignored.
_OWN_FILTER_FIELDS = {
    FilterField.ACCOUNT_ID,
    FilterField.CATEGORY_ID,
    FilterField.CURRENCY,
    FilterField.TXN_TYPE,
    FilterField.FREQUENCY,
    FilterField.RECURRING_ACTIVE,
    FilterField.AMOUNT,
}


def _measure_expr(measure):
    col_map = {
        MeasureField.AMOUNT: RecurringTransaction.amount,
        MeasureField.ID: RecurringTransaction.id,
    }
    col = col_map[measure.field]
    agg = measure.agg
    if agg is Aggregation.SUM:
        return func.coalesce(func.sum(col), 0).label("value")
    if agg is Aggregation.AVG:
        return func.coalesce(func.avg(col), 0).label("value")
    if agg is Aggregation.DISTINCT:
        return func.count(distinct(col)).label("value")
    if agg is Aggregation.COUNT:
        return func.count(col).label("value")
    raise ValueError(f"unsupported aggregation {agg!r}")


def _apply_filter(stmt, f):
    """Compile one of this source's OWN filters into a WHERE clause.

    Each branch is op-explicit and raises on an op the source does not
    support, so a future caller that skips ``validate()`` gets a loud
    error rather than a silently-dropped or mis-applied predicate
    (defense-in-depth; ``validate_against_catalog`` already op-checks).

    A field this source does not own (e.g. a dropped shared-canvas
    ``date``) still falls through to a bare ``return stmt`` — that is the
    Phase-5 shared-canvas drop contract, kept distinct from an op
    mismatch on an OWNED field.
    """
    field, op, value = f.field, f.op, f.value
    if field is FilterField.ACCOUNT_ID:
        if op is FilterOp.IN:
            return stmt.where(RecurringTransaction.account_id.in_(value))
        if op is FilterOp.EQ:
            return stmt.where(RecurringTransaction.account_id == value)
        raise ValueError(f"recurring: unsupported op {op.value!r} for account_id")
    if field is FilterField.CATEGORY_ID:
        if op is FilterOp.IN:
            return stmt.where(RecurringTransaction.category_id.in_(value))
        if op is FilterOp.EQ:
            return stmt.where(RecurringTransaction.category_id == value)
        raise ValueError(f"recurring: unsupported op {op.value!r} for category_id")
    if field is FilterField.CURRENCY:
        if op is FilterOp.IN:
            return stmt.where(Account.currency.in_(value))
        if op is FilterOp.EQ:
            return stmt.where(Account.currency == value)
        raise ValueError(f"recurring: unsupported op {op.value!r} for currency")
    if field is FilterField.TXN_TYPE:
        if op is FilterOp.IN:
            return stmt.where(RecurringTransaction.type.in_(value))
        if op is FilterOp.EQ:
            return stmt.where(RecurringTransaction.type == value)
        raise ValueError(f"recurring: unsupported op {op.value!r} for txn_type")
    if field is FilterField.FREQUENCY:
        if op is FilterOp.IN:
            return stmt.where(RecurringTransaction.frequency.in_(value))
        if op is FilterOp.EQ:
            return stmt.where(RecurringTransaction.frequency == value)
        raise ValueError(f"recurring: unsupported op {op.value!r} for frequency")
    if field is FilterField.RECURRING_ACTIVE:
        if op is FilterOp.EQ and isinstance(value, bool):
            return stmt.where(RecurringTransaction.is_active.is_(value))
        raise ValueError(
            f"recurring: recurring_active requires op 'eq' with a bool; "
            f"got op {op.value!r} value {value!r}"
        )
    if field is FilterField.AMOUNT:
        if op is FilterOp.BETWEEN:
            lo, hi = value
            return stmt.where(RecurringTransaction.amount.between(lo, hi))
        if op is FilterOp.GTE:
            return stmt.where(RecurringTransaction.amount >= value)
        if op is FilterOp.LTE:
            return stmt.where(RecurringTransaction.amount <= value)
        raise ValueError(f"recurring: unsupported op {op.value!r} for amount")
    return stmt  # field this source doesn't own → drop (shared-canvas contract)


class RecurringSource:
    key = "recurring"
    label = "Recurring"

    def dimensions(self) -> list[SourceDimension]:
        return list(_DIMENSIONS)

    def measures(self) -> list[SourceMeasure]:
        return list(_MEASURES)

    def filters(self) -> list[SourceFilter]:
        return list(_FILTERS)

    def validate(self, query: ReportsQuery) -> None:
        validate_against_catalog(self, query)

    async def build_rows(
        self, db: AsyncSession, org_id: int, query: ReportsQuery
    ) -> tuple[list[dict], dict]:
        dim_exprs: list[tuple[str, Any]] = []
        for dim in query.dimensions:
            key, expr = _DIM_EXPR[dim]
            dim_exprs.append((key, expr.label(key)))

        measure_expr = _measure_expr(query.measure)

        stmt = (
            select(*[expr for _, expr in dim_exprs], measure_expr)
            .select_from(RecurringTransaction)
            .join(Account, Account.id == RecurringTransaction.account_id)
            .join(Category, Category.id == RecurringTransaction.category_id)
            .where(RecurringTransaction.org_id == org_id)  # org-scope on recurring ONLY
        )

        for f in query.filters:
            if f.field in _OWN_FILTER_FIELDS:
                stmt = _apply_filter(stmt, f)
            # else: stray shared-canvas field (date, …) — silently dropped

        if dim_exprs:
            raw_group_cols = [_DIM_EXPR[dim][1] for dim in query.dimensions]
            stmt = stmt.group_by(*raw_group_cols)

        # ORDER BY — mirror reports_query_service so grouped + limited
        # results are deterministic, not arbitrarily ordered/truncated.
        sort = query.sort
        if sort is not None:
            if sort.by.value == "value":
                order_col = literal_column("value")
            else:  # by == "dimension"
                if not dim_exprs:
                    raise ValueError(
                        "sort.by='dimension' requires at least one dimension"
                    )
                order_col = literal_column(dim_exprs[0][0])
            order_col = order_col.asc() if sort.dir.value == "asc" else order_col.desc()
            stmt = stmt.order_by(order_col)
        elif dim_exprs:
            # Stable default order by value desc (matches transactions).
            stmt = stmt.order_by(literal_column("value").desc())

        # Deterministic tiebreaker so truncation on ties is stable across
        # runs. Only meaningful when grouping (multiple rows): a no-dimension
        # query is a single aggregate row where RecurringTransaction.id is
        # neither selected nor grouped, so an ORDER BY on it would be invalid SQL.
        if dim_exprs:
            stmt = stmt.order_by(func.min(RecurringTransaction.id).asc())

        stmt = stmt.limit(min(query.limit, MAX_LIMIT))

        started = time.perf_counter()
        result = await db.execute(stmt)
        rows = result.mappings().all()
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        out_rows = []
        for r in rows:
            d = {}
            for key, _ in dim_exprs:
                v = r.get(key)
                # frequency (and defensively txn_type) may come back as an
                # enum.Enum member; normalize to its plain string value so
                # JSON keys are stable strings ("monthly"), never a repr.
                if isinstance(v, _enum.Enum):
                    v = v.value
                d[key] = v
            val = r.get("value")
            if hasattr(val, "as_tuple"):  # Decimal-like
                try:
                    d["value"] = float(val)
                except Exception:  # pragma: no cover - defensive
                    d["value"] = str(val)
            else:
                d["value"] = val
            out_rows.append(d)

        meta = {
            "row_count": len(out_rows),
            "truncated": len(out_rows) >= query.limit,
            "query_ms": elapsed_ms,
        }
        return out_rows, meta


_INSTANCE: ReportSource = RecurringSource()
register(_INSTANCE)
