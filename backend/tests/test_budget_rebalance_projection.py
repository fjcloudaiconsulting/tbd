from decimal import Decimal

from app.services.budget_rebalance_service import _CategoryFact, _project_period_spend


def _fact(budget, avg, actual):
    return _CategoryFact(
        category_id=1,
        category_name="X",
        budget_amount=Decimal(budget),
        last_3mo_total=Decimal(avg) * 3,
        last_3mo_avg=Decimal(avg),
        current_mo_actual=Decimal(actual),
    )


def test_projection_uses_trend_when_month_is_early():
    # spent little so far; project at the 3-month run-rate
    assert _project_period_spend(_fact("100", "80", "20")) == Decimal("80.00")


def test_projection_uses_actual_when_already_above_trend():
    # already spent more than the average; project at the higher actual
    assert _project_period_spend(_fact("100", "80", "95")) == Decimal("95.00")
