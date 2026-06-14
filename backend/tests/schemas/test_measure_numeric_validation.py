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


def test_numeric_set_is_amount_and_balance():
    assert NUMERIC_MEASURE_FIELDS == {MeasureField.AMOUNT, MeasureField.BALANCE}
