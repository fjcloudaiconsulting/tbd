from decimal import Decimal

from app.schemas.budget_rebalance import BudgetRebalanceResponse


def test_response_carries_conservation_fields():
    r = BudgetRebalanceResponse(
        status="ok",
        period_start="2026-06-01",
        total_budget=Decimal("190.00"),
        total_suggested=Decimal("190.00"),
        uncovered_overspend=Decimal("0.00"),
        is_balanced=True,
    )
    assert r.total_budget == Decimal("190.00")
    assert r.is_balanced is True


def test_empty_no_surplus_is_a_valid_status():
    r = BudgetRebalanceResponse(status="empty_no_surplus")
    assert r.status == "empty_no_surplus"
    # defaults stay safe
    assert r.total_budget == Decimal("0")
    assert r.uncovered_overspend == Decimal("0")
    assert r.is_balanced is True
