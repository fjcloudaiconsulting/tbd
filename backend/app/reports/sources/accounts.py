"""Accounts source — balance / count over the accounts snapshot.

Accounts is a *snapshot* table: one row per account, with ``balance``
stored as a column. So this source compiles to a single ``Account JOIN
AccountType`` SELECT — no transaction reconstruction. ``org_id`` is
injected here (HARD requirement) and scoped on ``accounts.org_id`` only;
AccountType is reachable solely through the FK, so it needs no separate
org filter.

The compiler is structurally incapable of emitting SQL for any field
outside its own catalog (date / category_id / … are silently ignored),
which is what makes the shared-canvas date-bar drop a stray date filter
instead of erroring — the Phase-5 contract.
"""
from __future__ import annotations

import time
from typing import Any

from sqlalchemy import case, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountType
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
    SourceDimension("account", "Account", "account"),
    SourceDimension("account_type", "Account type", "account_type"),
    SourceDimension("currency", "Currency", "currency"),
    SourceDimension("account_active", "Status", "boolean"),
]

_MEASURES = [
    SourceMeasure("sum_balance", "Total balance", "sum", "balance", "currency"),
    SourceMeasure("avg_balance", "Average balance", "avg", "balance", "currency"),
    SourceMeasure("count_accounts", "Account count", "count", "id", "number"),
]

_FILTERS = [
    SourceFilter("account_id", "Account", ("in",), "account"),
    SourceFilter("account_type", "Account type", ("eq", "in"), "account_type"),
    SourceFilter("currency", "Currency", ("eq", "in"), "currency"),
    SourceFilter("account_active", "Status", ("eq",), "boolean"),
    SourceFilter("balance", "Balance", ("between", "gte", "lte"), "number"),
]

# Dimension → (response key, SQL expression). account_active normalizes
# the boolean to stable "Active"/"Inactive" strings in the SELECT via a
# CASE so we never depend on the driver returning a Python bool.
_DIM_EXPR = {
    Dimension.ACCOUNT: ("account", Account.name),
    Dimension.ACCOUNT_TYPE: ("account_type", AccountType.name),
    Dimension.CURRENCY: ("currency", Account.currency),
    Dimension.ACCOUNT_ACTIVE: (
        "account_active",
        case((Account.is_active.is_(True), "Active"), else_="Inactive"),
    ),
}

# Only the source's OWN filter fields are compiled. Anything else
# (date, category_id, txn_type, …) is silently ignored.
_OWN_FILTER_FIELDS = {
    FilterField.ACCOUNT_ID,
    FilterField.ACCOUNT_TYPE,
    FilterField.CURRENCY,
    FilterField.ACCOUNT_ACTIVE,
    FilterField.BALANCE,
}


def _measure_expr(measure):
    col_map = {
        MeasureField.BALANCE: Account.balance,
        MeasureField.ID: Account.id,
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
    field, op, value = f.field, f.op, f.value
    if field is FilterField.ACCOUNT_ID:
        if op is FilterOp.IN:
            return stmt.where(Account.id.in_(value))
        return stmt.where(Account.id == value)
    if field is FilterField.ACCOUNT_TYPE:
        if op is FilterOp.IN:
            return stmt.where(Account.account_type_id.in_(value))
        return stmt.where(Account.account_type_id == value)
    if field is FilterField.CURRENCY:
        if op is FilterOp.IN:
            return stmt.where(Account.currency.in_(value))
        return stmt.where(Account.currency == value)
    if field is FilterField.ACCOUNT_ACTIVE:
        return stmt.where(Account.is_active.is_(bool(value)))
    if field is FilterField.BALANCE:
        if op is FilterOp.BETWEEN:
            lo, hi = value
            return stmt.where(Account.balance.between(lo, hi))
        if op is FilterOp.GTE:
            return stmt.where(Account.balance >= value)
        if op is FilterOp.LTE:
            return stmt.where(Account.balance <= value)
    return stmt  # defensive: unreachable given _OWN_FILTER_FIELDS gate


class AccountsSource:
    key = "accounts"
    label = "Accounts"

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
            .select_from(Account)
            .join(AccountType, AccountType.id == Account.account_type_id)
            .where(Account.org_id == org_id)  # org-scope on accounts ONLY
        )

        for f in query.filters:
            if f.field in _OWN_FILTER_FIELDS:
                stmt = _apply_filter(stmt, f)
            # else: stray shared-canvas field (date, …) — silently dropped

        if dim_exprs:
            raw_group_cols = [_DIM_EXPR[dim][1] for dim in query.dimensions]
            stmt = stmt.group_by(*raw_group_cols)

        stmt = stmt.limit(min(query.limit, MAX_LIMIT))

        started = time.perf_counter()
        result = await db.execute(stmt)
        rows = result.mappings().all()
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        out_rows = []
        for r in rows:
            d = {}
            for key, _ in dim_exprs:
                d[key] = r.get(key)
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


_INSTANCE: ReportSource = AccountsSource()
register(_INSTANCE)
