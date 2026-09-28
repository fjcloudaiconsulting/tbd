"""TBD-556: over_budget is explicit, because percent_used is 0 at amount 0."""

import datetime
from decimal import Decimal

import pytest

from app.models.budget import Budget
from app.services.budget_service import _to_response


def _budget(amount: str) -> Budget:
    return Budget(
        id=1,
        category_id=1,
        amount=Decimal(amount),
        period_start=datetime.date(2026, 9, 1),
    )


@pytest.mark.parametrize(
    ("amount", "spent", "over"),
    [
        ("0", "25.00", True),  # the defect: drained to 0, then spent against
        ("0", "0", False),
        ("100", "100.00", False),  # exactly at budget is not over
        ("100", "100.04", True),  # rounds to 100.0%, still over
        ("100", "40.00", False),
    ],
)
def test_over_budget(amount, spent, over):
    resp = _to_response(_budget(amount), Decimal(spent))
    assert resp.over_budget is over


def test_zero_amount_percent_stays_finite():
    resp = _to_response(_budget("0"), Decimal("25.00"))
    assert resp.percent_used == 0.0
    assert resp.model_dump_json()  # no inf/NaN on the wire
