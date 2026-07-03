import random
from decimal import Decimal

from app.services.budget_rebalance_service import (
    _allocate_rebalance,
    _CategoryFact,
    _project_period_spend,
)


def _fact(cid, name, budget, avg, actual):
    return _CategoryFact(
        category_id=cid,
        category_name=name,
        budget_amount=Decimal(budget),
        last_3mo_total=Decimal(avg) * 3,
        last_3mo_avg=Decimal(avg),
        current_mo_actual=Decimal(actual),
    )


def _fact_proj(cid, budget, proj):
    """Build a fact whose projected spend is exactly ``proj`` (via actual)."""
    return _CategoryFact(
        category_id=cid,
        category_name=f"c{cid}",
        budget_amount=Decimal(str(budget)),
        last_3mo_total=Decimal("0"),
        last_3mo_avg=Decimal("0"),
        current_mo_actual=Decimal(str(proj)),
    )


def _assert_per_category_invariant(facts, suggestions):
    """No surplus (giver) category may be raised or pushed below its
    projected need, and the total must be conserved (zero-sum)."""
    proj = {f.category_id: _project_period_spend(f) for f in facts}
    budget = {f.category_id: f.budget_amount for f in facts}
    emitted = {s.category_id: s.suggested_amount for s in suggestions}
    total_suggested = sum(
        emitted.get(f.category_id, f.budget_amount) for f in facts
    )
    assert total_suggested == sum(f.budget_amount for f in facts)
    for s in suggestions:
        cid = s.category_id
        if budget[cid] - proj[cid] > 0:  # a giver (has surplus)
            assert s.delta_amount <= 0, f"giver {cid} was raised"
            assert s.suggested_amount >= proj[cid], (
                f"giver {cid} pushed below its projected need"
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


def test_rounding_residual_never_over_gives_or_reverses_a_giver():
    """Regression: the proportional split must never push a surplus
    category below its projected need, nor hand a surplus category a
    positive (wrong-direction) delta from a rounding residual.

    Both inputs below tripped the original "float the residual onto the
    last giver" implementation — the last giver either over-gave by a
    cent or received a negative give (its budget went UP with a bogus
    'covering overspend' reasoning). Largest-remainder apportionment
    fixes both while preserving zero-sum.
    """
    # Reviewer Bug 2: last giver (cid 3) previously got delta +0.01.
    facts = [
        _fact_proj(0, 303.98, 89.74),
        _fact_proj(1, 311.64, 154.14),
        _fact_proj(2, 229.69, 163.17),
        _fact_proj(3, 459.65, 453.99),  # tiny headroom, was the last giver
        _fact_proj(4, 444.51, 444.75),  # deficit 0.24
    ]
    suggestions, _ = _allocate_rebalance(facts, priority_ids=[4])
    _assert_per_category_invariant(facts, suggestions)

    # Reviewer Bug 1: last giver (cid 4) previously pushed 1 cent below.
    facts = [
        _fact_proj(0, 231.07, 215.06),
        _fact_proj(1, 156.87, 108.42),
        _fact_proj(2, 427.15, 105.04),
        _fact_proj(3, 21.10, 338.66),
        _fact_proj(4, 66.93, 56.81),
        _fact_proj(5, 147.16, 226.16),
    ]
    suggestions, _ = _allocate_rebalance(facts, priority_ids=[3, 5])
    _assert_per_category_invariant(facts, suggestions)


def test_per_category_invariant_holds_over_randomized_inputs():
    """Fuzz the allocator with seeded random budgets/projections; the
    per-category invariant (and zero-sum) must hold every time."""
    rng = random.Random(20260703)
    for _ in range(5000):
        n = rng.randint(2, 6)
        facts = [
            _fact_proj(
                i,
                round(rng.uniform(10, 500), 2),
                round(rng.uniform(10, 500), 2),
            )
            for i in range(n)
        ]
        priority = [
            f.category_id
            for f in facts
            if f.budget_amount < _project_period_spend(f)
        ]
        rng.shuffle(priority)
        suggestions, _ = _allocate_rebalance(facts, priority_ids=priority)
        _assert_per_category_invariant(facts, suggestions)
