from decimal import Decimal

from app.services.budget_rebalance_service import _CategoryFact, _allocate_rebalance


def _fact(cid, name, budget, avg, actual):
    return _CategoryFact(
        category_id=cid,
        category_name=name,
        budget_amount=Decimal(budget),
        last_3mo_total=Decimal(avg) * 3,
        last_3mo_avg=Decimal(avg),
        current_mo_actual=Decimal(actual),
    )


def test_exact_match_moves_surplus_to_deficit():
    # Transportation: budget 100, projected 90 -> 10 surplus
    # Bills: budget 90, projected 100 -> 10 deficit
    facts = [
        _fact(1, "Transportation", "100", "90", "90"),
        _fact(2, "Bills", "90", "100", "100"),
    ]
    suggestions, uncovered = _allocate_rebalance(facts, priority_ids=[2])
    by_cat = {s.category_id: s for s in suggestions}
    assert by_cat[1].suggested_amount == Decimal("90.00")   # gave its 10 surplus
    assert by_cat[2].suggested_amount == Decimal("100.00")  # covered its 10 deficit
    assert uncovered == Decimal("0.00")
    # conservation across ALL facts (emitted change, else original budget)
    emitted = {s.category_id: s.suggested_amount for s in suggestions}
    total = sum(emitted.get(f.category_id, f.budget_amount) for f in facts)
    assert total == Decimal("190.00")


def test_partial_cover_reports_uncovered_gap():
    # surplus 50 total, deficit 80 total -> 30 uncovered
    facts = [
        _fact(1, "Transportation", "100", "50", "50"),  # +50 surplus
        _fact(2, "Bills", "90", "130", "130"),           # -40 deficit
        _fact(3, "Food", "60", "100", "100"),            # -40 deficit
    ]
    # priority: cover Bills first, then Food
    suggestions, uncovered = _allocate_rebalance(facts, priority_ids=[2, 3])
    by_cat = {s.category_id: s for s in suggestions}
    assert by_cat[1].suggested_amount == Decimal("50.00")   # gave all 50 surplus
    assert by_cat[2].suggested_amount == Decimal("130.00")  # fully covered: 90 + 40
    assert by_cat[3].suggested_amount == Decimal("70.00")   # partial: 60 + 10 (only 10 left)
    assert uncovered == Decimal("30.00")                    # Food still 30 short

    # zero-sum: sum of new amounts over ALL facts == sum of budgets
    emitted = {s.category_id: s.suggested_amount for s in suggestions}
    total_suggested = sum(emitted.get(f.category_id, f.budget_amount) for f in facts)
    total_budget = sum(f.budget_amount for f in facts)
    assert total_suggested == total_budget  # 250
