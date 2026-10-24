"""BE-01: numeric report filter values are finite and of bounded size.

The driver renders a ``Decimal`` bind as a plain-notation literal, one
character per unit of exponent, so an amount or balance filter of
``"1E+100000"`` used to compile into a 100 KB statement. Filter bounds are
compared, never stored, so they keep more precision than ``Money`` allows.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.schemas.reports_query import Filter, FilterField, FilterOp, SankeyQuery

NUMERIC_FIELDS = [FilterField.AMOUNT, FilterField.BALANCE]
UNBOUNDED = ["1E+100000", "1e1000000000", "1E-100000", "NaN", "Infinity", "-Infinity",
             "1000000000000000"]


@pytest.mark.parametrize("field", NUMERIC_FIELDS)
@pytest.mark.parametrize("raw", UNBOUNDED)
def test_scalar_numeric_filter_rejects_unbounded_values(field, raw):
    with pytest.raises(PydanticValidationError, match="numeric filter value"):
        Filter(field=field, op=FilterOp.GTE, value=raw)


@pytest.mark.parametrize("field", NUMERIC_FIELDS)
@pytest.mark.parametrize("raw", UNBOUNDED)
def test_between_numeric_filter_rejects_unbounded_bounds(field, raw):
    with pytest.raises(PydanticValidationError, match="numeric filter value"):
        Filter(field=field, op=FilterOp.BETWEEN, value=["0", raw])


def test_in_amount_filter_rejects_unbounded_members():
    with pytest.raises(PydanticValidationError, match="numeric filter value"):
        Filter(field=FilterField.AMOUNT, op=FilterOp.IN, value=["10", "1E+100000"])


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("12.345", Decimal("12.345")),  # finer than a stored amount: still a fair bound
        (0.30000000000000004, Decimal("0.30000000000000004")),  # a JS float
        ("-9999999999.99", Decimal("-9999999999.99")),
        (500, Decimal("500")),
        ("1e3", Decimal("1E+3")),
        ("99999999999999.5", Decimal("99999999999999.5")),
    ],
)
def test_numeric_filter_keeps_accepting_ordinary_bounds(raw, expected):
    f = Filter(field=FilterField.AMOUNT, op=FilterOp.LTE, value=raw)
    assert f.value == expected
    f = Filter(field=FilterField.BALANCE, op=FilterOp.BETWEEN, value=[raw, raw])
    assert f.value == [expected, expected]


def test_sankey_filters_share_the_bound():
    with pytest.raises(PydanticValidationError, match="numeric filter value"):
        SankeyQuery.model_validate(
            {"filters": [{"field": "amount", "op": "gte", "value": "1E+100000"}]}
        )
