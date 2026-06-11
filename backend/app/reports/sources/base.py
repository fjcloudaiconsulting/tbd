"""Reports v3 — pluggable source layer.

A ``ReportSource`` answers three questions: which dimensions can you
group/filter by, which measures can you plot, and how do you build the
rows for a query. The registry dispatches the reports query AST on its
``dataset`` discriminator to the registered source. Phase 1 ships one
source (transactions) that delegates to the existing compiler; the
interface is what makes accounts / recurring / net-worth additive later.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.reports_query import QueryMeta, ReportsQuery


@dataclass(frozen=True)
class SourceDimension:
    key: str       # matches the AST Dimension value, e.g. "category"
    label: str     # human label for the editor, e.g. "Category"
    kind: str      # control hint: category|account|status|type|tag|time|account_type


@dataclass(frozen=True)
class SourceMeasure:
    key: str       # stable id for the editor, e.g. "sum_amount"
    label: str     # human label, e.g. "Total amount"
    agg: str       # sum|count|avg|distinct
    field: str     # amount|id|category_id|account_id (AST MeasureField value)
    format: str    # currency|number|percent


@runtime_checkable
class ReportSource(Protocol):
    key: str
    label: str

    def dimensions(self) -> list[SourceDimension]: ...

    def measures(self) -> list[SourceMeasure]: ...

    async def build_rows(
        self, db: AsyncSession, org_id: int, query: ReportsQuery
    ) -> tuple[list[dict], QueryMeta]: ...
