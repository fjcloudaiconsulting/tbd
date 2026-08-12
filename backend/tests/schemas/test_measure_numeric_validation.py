import pytest
from pydantic import ValidationError

from app.schemas.reports_query import (
    Aggregation, Measure, MeasureField, NUMERIC_MEASURE_FIELDS,
)


def test_sum_balance_is_numerically_sane():
    m = Measure(agg=Aggregation.SUM, field=MeasureField.BALANCE)
    assert m.field is MeasureField.BALANCE


def test_sum_id_still_rejected_at_pydantic():
    with pytest.raises(ValidationError):
        Measure(agg=Aggregation.SUM, field=MeasureField.ID)


def test_numeric_measure_field_set_is_pinned():
    # Renamed from test_numeric_set_is_amount_balance_networth (TBD-170): the
    # old name became a lie the moment the set grew past those three.
    #
    # net_worth and utilization_pct are NOMINAL numeric fields — their sources'
    # build_rows compute the value and ignore the generic agg machinery — but
    # they must be here or Measure._validate_agg_field 422s them before the
    # source is ever consulted.
    assert NUMERIC_MEASURE_FIELDS == {
        MeasureField.AMOUNT,
        MeasureField.BALANCE,
        MeasureField.NET_WORTH,
        MeasureField.UTILIZATION_PCT,
        MeasureField.OUTSTANDING,
        MeasureField.CREDIT_LIMIT,
    }
