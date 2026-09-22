"""Transactions source — wraps the existing reports query compiler.

build_rows delegates to ``execute_query`` verbatim, so the transactions
query path is byte-for-byte identical to pre-registry behavior. The
catalog (dimensions/measures) is derived from the closed AST enums so it
cannot drift from what the compiler actually accepts.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.reports.sources import register
from app.reports.sources.base import (
    ReportSource, SourceDimension, SourceFilter, SourceMeasure,
    validate_against_catalog,
)
from app.schemas.reports_query import Aggregation, MeasureField, ReportsQuery
from app.services.reports_query_service import execute_query

_DIMENSIONS = [
    SourceDimension("category", "Category", "category"),
    SourceDimension("category_master", "Category group", "category"),
    SourceDimension("account", "Account", "account"),
    SourceDimension("currency", "Currency", "currency"),
    SourceDimension("account_type", "Account type", "account_type"),
    SourceDimension("tag", "Tag", "tag"),
    SourceDimension("txn_type", "Type", "type"),
    SourceDimension("status", "Status", "status"),
    SourceDimension("month", "Month", "time"),
    SourceDimension("week", "Week", "time"),
    SourceDimension("day", "Day", "time"),
]

_MEASURES = [
    SourceMeasure("sum_amount", "Total amount", "sum", "amount", "currency"),
    SourceMeasure("avg_amount", "Average amount", "avg", "amount", "currency"),
    SourceMeasure("count_rows", "Transaction count", "count", "id", "number"),
    # TBD-553. ADDITIVE, not a re-signing of ``sum_amount`` -- see
    # ``signed_amount_expr`` / ``MeasureField.NET_AMOUNT`` for why. Only
    # ``sum`` is coherent over it; see ``_DECLARED_AGG`` below.
    SourceMeasure("sum_net_amount", "Net", "sum", "net_amount", "currency"),
]

# The declared agg for the one field this source restricts. ⚠ NOT exhaustive
# over every published (field, agg) pair -- unlike credit_utilization's
# mapping, that shape is inexpressible here: ``_DECLARED_AGG`` maps
# field -> ONE Aggregation, and this source publishes BOTH ``sum_amount`` and
# ``avg_amount`` over ``amount``, so whichever agg were chosen for AMOUNT
# would 422 the other published measure. ``count(amount)`` / ``avg(id)`` have
# been unreachable from the editor since TBD-402 and survive only in
# pre-TBD-402 saved layouts, which ``UNSUPPORTED_MEASURE_KEY`` deliberately
# renders-and-flags rather than breaks.
_DECLARED_AGG = {
    MeasureField.NET_AMOUNT: Aggregation.SUM,
}

_FILTERS = [
    SourceFilter("date", "Date", ("between", "gte", "lte"), "time"),
    SourceFilter("amount", "Amount", ("between", "gte", "lte", "eq"), "amount"),
    SourceFilter("category_id", "Category", ("eq", "in"), "category"),
    SourceFilter("account_id", "Account", ("eq", "in"), "account"),
    SourceFilter("currency", "Currency", ("eq", "in"), "currency"),
    # TBD-471. ⚠ The account_type DIMENSION is published; the account_type
    # FILTER deliberately is NOT. A dimension reaches the editor with zero
    # frontend code (the picker is catalog-driven), whereas a filter needs a
    # control. ``account_id`` already covers explicit account selection, so the
    # filter would buy nothing and would repeat TBD-507's currency filter, which
    # renders zero pixels to this day.
    # ⚠ ``transfer`` below is published WITHOUT a control in PR 1 and is
    # therefore in exactly that state -- deliberately and temporarily. PR 2 adds
    # the control in the same ticket. If PR 2 does not land, this filter is the
    # third unreachable one and should be reconsidered, not left to settle.
    SourceFilter("transfer", "Transfers", ("eq",), "boolean"),
    SourceFilter("txn_type", "Type", ("eq", "in"), "type"),
    SourceFilter("status", "Status", ("eq",), "status"),
    SourceFilter("tag_name", "Tag", ("eq", "in"), "tag"),
]


class TransactionsSource:
    key = "transactions"
    label = "Transactions"

    def dimensions(self) -> list[SourceDimension]:
        return list(_DIMENSIONS)

    def measures(self) -> list[SourceMeasure]:
        return list(_MEASURES)

    def filters(self) -> list[SourceFilter]:
        return list(_FILTERS)

    def validate(self, query: ReportsQuery) -> None:
        validate_against_catalog(self, query)

        declared = _DECLARED_AGG.get(query.measure.field)
        if declared is not None and query.measure.agg is not declared:
            raise ValueError(
                f"source 'transactions' measure "
                f"{query.measure.field.value!r} must use agg "
                f"{declared.value!r}, not {query.measure.agg.value!r}"
            )

    async def build_rows(
        self, db: AsyncSession, org_id: int, query: ReportsQuery
    ) -> tuple[list[dict], dict]:  # meta dict carries row_count, truncated, query_ms — coerced to QueryMeta at the router
        return await execute_query(db, query, org_id=org_id)


_INSTANCE: ReportSource = TransactionsSource()
register(_INSTANCE)
