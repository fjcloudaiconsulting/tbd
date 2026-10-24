"""BE-11: a report ``in`` filter carries at most ``MAX_IN_VALUES`` values.

Every value is a bind the statement compiles and the driver escapes, so an
uncapped list made the cost of one query grow with the request body.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.schemas.reports_query import (
    MAX_IN_VALUES,
    Filter,
    FilterField,
    FilterOp,
    SankeyQuery,
)

IN_FIELDS = [
    (FilterField.CATEGORY_ID, lambda i: i),
    (FilterField.ACCOUNT_ID, lambda i: str(i)),
    (FilterField.TAG_NAME, lambda i: f"tag-{i}"),
    (FilterField.AMOUNT, lambda i: f"{i}.50"),
]


@pytest.mark.parametrize("field, make", IN_FIELDS, ids=[f.value for f, _ in IN_FIELDS])
def test_in_filter_accepts_up_to_the_cap_and_rejects_one_more(field, make):
    assert MAX_IN_VALUES == 200
    Filter(field=field, op=FilterOp.IN, value=[make(i) for i in range(MAX_IN_VALUES)])
    with pytest.raises(PydanticValidationError, match=f"at most {MAX_IN_VALUES} values"):
        Filter(field=field, op=FilterOp.IN, value=[make(i) for i in range(MAX_IN_VALUES + 1)])


def test_in_cap_applies_to_sankey_filters_too():
    with pytest.raises(PydanticValidationError, match="at most"):
        SankeyQuery.model_validate({"filters": [
            {"field": "tag_name", "op": "in", "value": [f"t{i}" for i in range(MAX_IN_VALUES + 1)]}
        ]})
