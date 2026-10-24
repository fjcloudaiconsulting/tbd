"""BE-27: free-text request fields are bounded by their column lengths."""
from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.schemas.category import CategoryCreate, CategoryUpdate
from app.schemas.recurring import RecurringCreate, RecurringUpdate

_RECURRING = {
    "account_id": 1, "category_id": 1, "amount": "10.00", "type": "expense",
    "frequency": "monthly", "next_due_date": "2026-05-01",
}

CASES = [
    (CategoryCreate, {}, "name", 100),
    (CategoryCreate, {"name": "Food"}, "description", 255),
    (CategoryUpdate, {}, "name", 100),
    (CategoryUpdate, {}, "description", 255),
    (RecurringCreate, _RECURRING, "description", 255),
    (RecurringUpdate, {}, "description", 255),
]


@pytest.mark.parametrize(
    "model, base, field, limit", CASES, ids=[f"{m.__name__}.{f}" for m, _, f, _ in CASES]
)
def test_free_text_matches_its_column(model, base, field, limit):
    model.model_validate({**base, field: "x" * limit})
    with pytest.raises(PydanticValidationError) as exc:
        model.model_validate({**base, field: "x" * (limit + 1)})
    assert exc.value.errors()[0]["loc"] == (field,)


def test_free_text_limits_equal_the_model_columns():
    from app.models.category import Category
    from app.models.recurring import RecurringTransaction

    assert Category.__table__.c.name.type.length == 100
    assert Category.__table__.c.description.type.length == 255
    assert RecurringTransaction.__table__.c.description.type.length == 255
