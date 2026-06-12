import pytest

from app.reports import sources as source_registry
from app.reports.sources.base import ReportSource, SourceDimension, SourceMeasure
from app.schemas.reports_query import Aggregation, MeasureField


def test_source_dimension_and_measure_are_simple_value_objects():
    dim = SourceDimension(key="category", label="Category", kind="category")
    meas = SourceMeasure(key="sum_amount", label="Total", agg="sum", field="amount", format="currency")
    assert dim.key == "category" and dim.kind == "category"
    assert meas.agg == "sum" and meas.format == "currency"


def test_registry_resolves_transactions_and_rejects_unknown():
    src = source_registry.get_source("transactions")
    assert src.key == "transactions"
    assert "transactions" in {s.key for s in source_registry.all_sources()}
    with pytest.raises(KeyError):
        source_registry.get_source("nope")


def test_transactions_source_catalog_matches_ast_enums():
    from app.schemas.reports_query import Dimension as AstDimension

    src = source_registry.get_source("transactions")
    dim_keys = {d.key for d in src.dimensions()}
    # Every catalog dimension is a real AST Dimension value (no typos).
    assert dim_keys.issubset({d.value for d in AstDimension})
    assert {"category", "account", "status", "txn_type", "month"}.issubset(dim_keys)
    measure_keys = {m.key for m in src.measures()}
    assert {"sum_amount", "count_rows"}.issubset(measure_keys)
    sum_amount = next(m for m in src.measures() if m.key == "sum_amount")
    assert sum_amount.format == "currency" and sum_amount.field == "amount"

    agg_values = {a.value for a in Aggregation}
    field_values = {f.value for f in MeasureField}
    for m in src.measures():
        assert m.agg in agg_values, f"bad agg: {m.agg}"
        assert m.field in field_values, f"bad field: {m.field}"

    # exact catalog contract (not just subset)
    assert dim_keys == {"category", "category_master", "account", "tag",
                        "txn_type", "status", "month", "week", "day"}
    assert {m.key for m in src.measures()} == {"sum_amount", "avg_amount", "count_rows"}
    by_key = {m.key: m for m in src.measures()}
    assert (by_key["avg_amount"].agg, by_key["avg_amount"].field, by_key["avg_amount"].format) == ("avg", "amount", "currency")
    assert (by_key["count_rows"].agg, by_key["count_rows"].field, by_key["count_rows"].format) == ("count", "id", "number")


def test_every_dataset_enum_value_has_a_registered_source():
    from app.reports import sources as source_registry
    from app.schemas.reports_query import Dataset
    registered = {s.key for s in source_registry.all_sources()}
    enum_values = {d.value for d in Dataset}
    assert enum_values == registered, (
        f"Dataset enum and registry drifted: "
        f"enum-only={enum_values - registered}, registry-only={registered - enum_values}"
    )


@pytest.mark.asyncio
async def test_run_query_dispatches_via_registry(monkeypatch):
    """The route resolves the source from the AST dataset and calls its
    build_rows — proving the indirection, not a direct execute_query call."""
    from app.routers import reports as reports_router
    from app.schemas.reports_query import (
        Aggregation, Dataset, Dimension, Measure, MeasureField, ReportsQuery,
    )

    called = {}

    async def fake_build_rows(self, db, org_id, query):
        called["org_id"] = org_id
        called["dataset"] = query.dataset.value
        return ([{"category": "Food", "value": 12}], {"row_count": 1, "truncated": False, "query_ms": 1})

    src = reports_router.get_source("transactions")
    # TransactionsSource.build_rows is defined on the class, not the instance.
    # Patching on the class means Python's descriptor protocol passes self as
    # the first argument, so fake_build_rows takes (self, db, org_id, query).
    monkeypatch.setattr(type(src), "build_rows", fake_build_rows)

    ast = ReportsQuery(
        dataset=Dataset.TRANSACTIONS,
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
        dimensions=[Dimension.CATEGORY],
    )
    rows, meta = await reports_router._run_source_query(object(), ast, org_id=42)
    assert called == {"org_id": 42, "dataset": "transactions"}
    assert rows[0]["category"] == "Food" and meta["row_count"] == 1
