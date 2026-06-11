from app.reports.sources.base import ReportSource, SourceDimension, SourceMeasure


def test_source_dimension_and_measure_are_simple_value_objects():
    dim = SourceDimension(key="category", label="Category", kind="category")
    meas = SourceMeasure(key="sum_amount", label="Total", agg="sum", field="amount", format="currency")
    assert dim.key == "category" and dim.kind == "category"
    assert meas.agg == "sum" and meas.format == "currency"
