"""BE-22: the lists inside a plan's params are bounded.

The analytic engine runs synchronously in the request and iterates every
month x recurring row x event, so its inputs are capped like the horizon is.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.schemas.scenario import (
    MAX_CONTRIBUTION_CURVE_STEPS,
    MAX_CUSTOM_EVENTS,
    MAX_EVENT_CATEGORY_IDS,
    MAX_TRIP_EXTRAS,
    CustomParams,
    RetirementParams,
    TripParams,
)
from app.services.scenario_engine import RecurringSnapshot, _RecurringFilter


def _trip(n_extras: int) -> dict:
    return {
        "scenario_type": "trip", "destination": "Lisbon", "start_date": "2027-05-01",
        "duration_days": 7, "currency": "EUR", "transport_cost": "300",
        "accommodation_per_night": "90", "daily_budget": "60", "source_account_id": 1,
        "one_off_extras": [
            {"label": f"x{i}", "amount": "10", "on_date": "2027-05-02"} for i in range(n_extras)
        ],
    }


def _retirement(n_steps: int) -> dict:
    return {
        "scenario_type": "retirement", "target_retirement_date": "2066-01-01",
        "currency": "EUR", "monthly_contribution": "500", "contribution_account_id": 1,
        "target_balance": "500000", "annual_return_pct": "5",
        "contribution_curve": [
            {"from": f"{2027 + i // 12}-{i % 12 + 1:02d}-01", "monthly": "600"}
            for i in range(n_steps)
        ],
    }


def _custom(events: list[dict]) -> dict:
    return {"scenario_type": "custom", "label": "Sabbatical", "events": events}


_ONE_OFF = {"type": "one_off_expense", "month": 1, "amount": "100", "account_id": 1}


@pytest.mark.parametrize(
    "model, build, cap",
    [
        (TripParams, _trip, MAX_TRIP_EXTRAS),
        (RetirementParams, _retirement, MAX_CONTRIBUTION_CURVE_STEPS),
        (CustomParams, lambda n: _custom([_ONE_OFF] * n), MAX_CUSTOM_EVENTS),
        (
            CustomParams,
            lambda n: _custom([{"type": "expense_off", "from_month": 0,
                                "category_ids": list(range(n))}]),
            MAX_EVENT_CATEGORY_IDS,
        ),
    ],
    ids=["trip-extras", "retirement-curve", "custom-events", "expense_off-category_ids"],
)
def test_param_list_accepts_its_cap_and_rejects_one_more(model, build, cap):
    model.model_validate(build(cap))
    with pytest.raises(PydanticValidationError, match="at most"):
        model.model_validate(build(cap + 1))


def test_caps_are_the_documented_values():
    assert (MAX_TRIP_EXTRAS, MAX_CONTRIBUTION_CURVE_STEPS, MAX_CUSTOM_EVENTS,
            MAX_EVENT_CATEGORY_IDS) == (50, 480, 100, 200)


def test_expense_off_category_ids_are_deduplicated_in_order():
    params = CustomParams.model_validate(
        _custom([{"type": "expense_off", "from_month": 0, "category_ids": [5, 3, 5, 3, 7]}])
    )
    assert params.events[0].category_ids == [5, 3, 7]


def test_recurring_filter_category_scope_is_unchanged():
    """The engine's ``category_ids`` lookup is now a set; same answers."""
    f = _RecurringFilter([
        {"type": "expense_off", "from_month": 0, "to_month": 3, "category_ids": [5, 6]},
    ])

    def snap(category_id):
        return RecurringSnapshot(
            id=1, account_id=1, amount=Decimal("10"), type="expense",
            frequency="monthly", next_due_date=None, category_id=category_id,
        )

    assert f.should_apply(snap(5), 5, 2) is False
    assert f.should_apply(snap(99), 99, 2) is True
    assert f.should_apply(snap(5), 5, 4) is True  # outside the range
    assert _RecurringFilter([{"type": "expense_off", "from_month": 0}]).should_apply(
        snap(99), 99, 0
    ) is False  # no category_ids: every expense
