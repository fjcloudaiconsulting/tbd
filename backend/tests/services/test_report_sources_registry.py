from app.reports.sources.base import ReportSource, SourceDimension, SourceMeasure


def test_source_dimension_and_measure_are_simple_value_objects():
    dim = SourceDimension(key="category", label="Category", kind="category")
    meas = SourceMeasure(key="sum_amount", label="Total", agg="sum", field="amount", format="currency")
    assert dim.key == "category" and dim.kind == "category"
    assert meas.agg == "sum" and meas.format == "currency"


import pytest
from app.reports import sources as source_registry


def test_registry_resolves_transactions_and_rejects_unknown():
    src = source_registry.get_source("transactions")
    assert src.key == "transactions"
    assert "transactions" in {s.key for s in source_registry.all_sources()}
    with pytest.raises(KeyError):
        source_registry.get_source("nope")


def test_transactions_source_catalog_matches_ast_enums():
    from app.reports import sources as source_registry
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
