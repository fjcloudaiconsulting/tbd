"""Guard against the two-copy enum drift the shared module exists to kill."""
from app.schemas import reports_query, report_layout


def test_shared_enum_atoms_are_the_same_object():
    # After consolidation both modules re-export the SAME enum class.
    assert reports_query.Dataset is report_layout.Dataset
    assert reports_query.Dimension is report_layout.Dimension
    assert reports_query.MeasureField is report_layout.MeasureField
    assert reports_query.Aggregation is report_layout.Aggregation


def test_dataset_values():
    assert {d.value for d in reports_query.Dataset} == {"transactions", "accounts"}


def test_accounts_dataset_and_new_dimensions_present():
    from app.schemas.reports_query import Dataset, Dimension, MeasureField
    assert "accounts" in {d.value for d in Dataset}
    assert {"account_type", "currency", "account_active"}.issubset({d.value for d in Dimension})
    assert "balance" in {f.value for f in MeasureField}
