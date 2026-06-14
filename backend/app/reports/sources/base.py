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

from app.schemas.reports_query import ReportsQuery


@dataclass(frozen=True)
class SourceDimension:
    key: str       # matches the AST Dimension value, e.g. "category"
    label: str     # human label for the editor, e.g. "Category"
    kind: str      # control hint: category|account|status|type|tag|time|account_type|currency|boolean


@dataclass(frozen=True)
class SourceMeasure:
    key: str       # stable id for the editor, e.g. "sum_amount"
    label: str     # human label, e.g. "Total amount"
    agg: str       # sum|count|avg|distinct
    field: str     # amount|id|category_id|account_id (AST MeasureField value)
    format: str    # currency|number|percent


# Filter fields the shared canvas date-bar can stamp onto any widget. A
# source that does not publish one of these (e.g. a date-less accounts
# source) drops the stray filter at build time rather than 422-ing.
SHARED_CANVAS_FILTER_FIELDS = frozenset({"date", "account_id", "category_id"})


@dataclass(frozen=True)
class SourceFilter:
    field: str            # AST FilterField value, e.g. "currency"
    label: str            # human label for the editor
    ops: tuple[str, ...]  # allowed FilterOp values, e.g. ("eq", "in")
    kind: str             # control hint: account|account_type|currency|boolean|...


@runtime_checkable
class ReportSource(Protocol):
    key: str
    label: str

    def dimensions(self) -> list[SourceDimension]: ...

    def measures(self) -> list[SourceMeasure]: ...

    def filters(self) -> list[SourceFilter]: ...

    def validate(self, query: ReportsQuery) -> None:
        """Raise ValueError if the AST references a dimension / measure field /
        filter field this source does not publish. Shared-canvas fields that
        don't apply to this source are dropped, not rejected."""
        ...

    async def build_rows(
        self, db: AsyncSession, org_id: int, query: ReportsQuery
    ) -> tuple[list[dict], dict]: ...  # meta dict carries row_count, truncated, query_ms — coerced to QueryMeta at the router


def validate_against_catalog(source: "ReportSource", query: ReportsQuery) -> None:
    dim_keys = {d.key for d in source.dimensions()}
    for dim in query.dimensions:
        if dim.value not in dim_keys:
            raise ValueError(
                f"source {source.key!r} does not support dimension {dim.value!r}"
            )
    measure_fields = {m.field for m in source.measures()}
    if query.measure.field.value not in measure_fields:
        raise ValueError(
            f"source {source.key!r} does not support measure field "
            f"{query.measure.field.value!r}"
        )
    # Two-tier filter validation:
    #   1) a field the source publishes → also enforce the OP against that
    #      SourceFilter's allowed ops, so a published field with an op the
    #      source can't compile is rejected at 422 rather than silently
    #      dropped (or worse, mis-applied) in build_rows.
    #   2) else a shared-canvas field → dropped at build time (no op-check).
    #   3) else → reject the unknown field.
    filters_by_field = {sf.field: sf for sf in source.filters()}
    for f in query.filters:
        sf = filters_by_field.get(f.field.value)
        if sf is not None:
            if f.op.value not in sf.ops:
                raise ValueError(
                    f"source {source.key!r} filter {f.field.value!r} does not "
                    f"support op {f.op.value!r}"
                )
            continue
        if f.field.value in SHARED_CANVAS_FILTER_FIELDS:
            continue  # shared-bar artifact → dropped at build time
        raise ValueError(
            f"source {source.key!r} does not support filter field {f.field.value!r}"
        )
