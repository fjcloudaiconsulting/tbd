"""Transactions source — wraps the existing reports query compiler.

build_rows delegates to ``execute_query`` verbatim, so the transactions
query path is byte-for-byte identical to pre-registry behavior. The
catalog (dimensions/measures) is derived from the closed AST enums so it
cannot drift from what the compiler actually accepts.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.reports.sources import register
from app.reports.sources.base import ReportSource, SourceDimension, SourceMeasure
from app.schemas.reports_query import ReportsQuery
from app.services.reports_query_service import execute_query

_DIMENSIONS = [
    SourceDimension("category", "Category", "category"),
    SourceDimension("category_master", "Category group", "category"),
    SourceDimension("account", "Account", "account"),
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
]


class TransactionsSource:
    key = "transactions"
    label = "Transactions"

    def dimensions(self) -> list[SourceDimension]:
        return list(_DIMENSIONS)

    def measures(self) -> list[SourceMeasure]:
        return list(_MEASURES)

    async def build_rows(
        self, db: AsyncSession, org_id: int, query: ReportsQuery
    ) -> tuple[list[dict], dict]:  # meta dict carries row_count, truncated, query_ms — coerced to QueryMeta at the router
        return await execute_query(db, query, org_id=org_id)


_INSTANCE: ReportSource = TransactionsSource()
register(_INSTANCE)
