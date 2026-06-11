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
