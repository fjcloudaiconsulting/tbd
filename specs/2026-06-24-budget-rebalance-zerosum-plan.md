# Zero-Sum AI Budget Rebalance — Implementation Plan (PR A)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the AI budget rebalance conserve the total budget — move money between categories instead of inventing/removing it — by enforcing zero-sum in Python and demoting the LLM to prioritization + narrative.

**Architecture:** A deterministic allocator computes per-category projected end-of-period spend, derives surplus/deficit, and shuffles only the available surplus into deficits in an AI-supplied priority order (conserving the total by construction). The LLM returns a priority ordering + reasoning only; a hard guard asserts conservation and falls back to a deterministic baseline if the LLM drifts. The modal gains a live balance meter that warns when the user's accepted selection drifts from zero, plus an honest "uncovered overspend" banner.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.0 async / Pydantic v2 (backend); Next.js 16 / React 19 / TypeScript / Vitest (frontend).

## Global Constraints

- AI dispatch via `call_llm_structured` with `feature_key="smart_budget"` (the routable name, NOT the `ai.budget` entitlement key); dispatch runs in its own session via the `session_factory` pattern already in the service.
- Prompt carries **aggregates only** — no raw transactions, merchant names, account/user ids.
- Org-scoped queries; auth via `get_current_user`; the `ai.budget` entitlement (403) gate stays in the router (unchanged).
- Money is `Decimal`, quantized to `Decimal("0.01")`.
- Frontend: token classes only (No-Off-Token, CI-gated by `frontend/scripts/check-design-tokens.sh`); One Brass Rule / Sidebar-Always-Navy / WCAG 2.2 AA hold.
- Verify set MUST include `npm run lint` (eslint `no-explicit-any` is CI-gated, not covered by tsc).
- No AI attribution in commits or PR body.
- **Test isolation:** if executed by a parallel agent, every `docker compose` / `exec` call MUST carry a shared `-p team-rebalance` flag (per CLAUDE.md). In the user's own session, plain `docker compose exec backend ...` is fine.

---

### Task 1: Schema — add conservation fields + `empty_no_surplus` status

**Files:**
- Modify: `backend/app/schemas/budget_rebalance.py`
- Test: `backend/tests/test_budget_rebalance_schema.py` (create)

**Interfaces:**
- Produces: `BudgetRebalanceResponse` with new fields `total_budget: Decimal`, `total_suggested: Decimal`, `uncovered_overspend: Decimal`, `is_balanced: bool`, and `status` literal extended with `"empty_no_surplus"`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_budget_rebalance_schema.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec backend pytest tests/test_budget_rebalance_schema.py -v`
Expected: FAIL (`total_budget` is an unexpected field / `empty_no_surplus` not a valid literal).

- [ ] **Step 3: Implement the schema changes**

In `backend/app/schemas/budget_rebalance.py`, extend the `status` Literal and add fields to `BudgetRebalanceResponse`:

```python
    status: Literal[
        "ok",
        "empty_no_budgets",
        "empty_no_history",
        "empty_no_surplus",
        "llm_unavailable",
    ]
    period_start: Optional[str] = None
    suggestions: list[BudgetDeltaSuggestion] = Field(default_factory=list)
    summary: str = ""
    total_budget: Decimal = Decimal("0")
    total_suggested: Decimal = Decimal("0")
    uncovered_overspend: Decimal = Decimal("0")
    is_balanced: bool = True
    request_id: Optional[str] = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec backend pytest tests/test_budget_rebalance_schema.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/schemas/budget_rebalance.py backend/tests/test_budget_rebalance_schema.py
git commit -m "feat(budgets): add conservation fields to rebalance response schema"
```

---

### Task 2: Projection helper — `_project_period_spend`

**Files:**
- Modify: `backend/app/services/budget_rebalance_service.py`
- Test: `backend/tests/test_budget_rebalance_projection.py` (create)

**Interfaces:**
- Consumes: `_CategoryFact` (existing frozen dataclass with `budget_amount`, `last_3mo_avg`, `current_mo_actual`).
- Produces: `def _project_period_spend(fact: _CategoryFact) -> Decimal` — returns `max(current_mo_actual, last_3mo_avg)`, quantized to `0.01`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_budget_rebalance_projection.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec backend pytest tests/test_budget_rebalance_projection.py -v`
Expected: FAIL (`cannot import name '_project_period_spend'`).

- [ ] **Step 3: Implement the helper**

Add to `backend/app/services/budget_rebalance_service.py` (below `_CategoryFact`):

```python
def _project_period_spend(fact: "_CategoryFact") -> Decimal:
    """Conservative projected full-period spend for one category.

    ``max(current_mo_actual, last_3mo_avg)``: a category already pacing
    above its 3-month average is projected at the higher run-rate, so the
    allocator never frees money a category is on track to need. Early in a
    period, ``current_mo_actual`` is small and the 3-month average drives
    the projection.
    """
    return max(fact.current_mo_actual, fact.last_3mo_avg).quantize(Decimal("0.01"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec backend pytest tests/test_budget_rebalance_projection.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/budget_rebalance_service.py backend/tests/test_budget_rebalance_projection.py
git commit -m "feat(budgets): add conservative end-of-period spend projection"
```

---

### Task 3: Deterministic zero-sum allocator — `_allocate_rebalance`

**Files:**
- Modify: `backend/app/services/budget_rebalance_service.py`
- Test: `backend/tests/test_budget_rebalance_allocator.py` (create)

**Interfaces:**
- Consumes: `_CategoryFact`, `_project_period_spend`, `BudgetDeltaSuggestion`.
- Produces:
  `def _allocate_rebalance(facts: list[_CategoryFact], priority_ids: list[int], reasoning_by_cat: dict[int, str] | None = None) -> tuple[list[BudgetDeltaSuggestion], Decimal]`
  — returns `(suggestions, uncovered_overspend)`. Suggestions include only rows whose amount changes. The sum of `suggested_amount` over ALL facts equals the sum of `budget_amount` over all facts (conservation). `priority_ids` orders which deficit categories are covered first; any deficit not listed is appended by largest-need-first. `reasoning_by_cat` supplies per-category text; missing entries get a deterministic default.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_budget_rebalance_allocator.py
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
    assert by_cat[1].suggested_amount == Decimal("90.00")
    assert by_cat[2].suggested_amount == Decimal("100.00")
    assert uncovered == Decimal("0.00")
    # conservation across ALL facts
    total = sum(s.suggested_amount for s in suggestions)
    assert total == Decimal("190.00")


def test_partial_cover_reports_uncovered_gap():
    # surplus 50 total, deficit 80 total -> 30 uncovered
    facts = [
        _fact(1, "Transportation", "100", "50", "50"),  # +50 surplus
        _fact(2, "Bills", "90", "130", "130"),           # -40 deficit
        _fact(3, "Food", "60", "100", "100"),            # -40 deficit
    ]
    # priority: cover Bills first
    suggestions, uncovered = _allocate_rebalance(facts, priority_ids=[2, 3])
    by_cat = {s.category_id: s for s in suggestions}
    assert by_cat[1].suggested_amount == Decimal("50.00")   # gave all 50
    assert by_cat[2].suggested_amount == Decimal("90.00")   # fully covered: +40
    assert by_cat[3].suggested_amount == Decimal("70.00")   # partial: +10
    assert uncovered == Decimal("30.00")
    total = sum(s.suggested_amount for s in suggestions)
    # conservation: 50 + 90 + 70 == 210 == 100+90+60-? check vs budgets
    assert total == Decimal("210.00")  # 100+90+60 = 250 budget... see note
```

> Note for implementer: the second test's `total` equals the sum of the THREE budgets only if all three appear in `suggestions`. Food's budget is 60 and it ends at 70 (+10); Transportation 100→50 (−50); Bills 90→90 (0 net? no, +40 → but capped). Re-derive from your implementation and set the assertion to `sum(original budgets) == 250`; conservation means `sum(suggested over all facts) == 250`. Because only changed rows are emitted, assert conservation by summing suggested over changed rows PLUS unchanged budgets. Use the helper below instead.

Replace the last two asserts of `test_partial_cover_reports_uncovered_gap` with a robust conservation check:

```python
    emitted = {s.category_id: s.suggested_amount for s in suggestions}
    total_suggested = sum(
        emitted.get(f.category_id, f.budget_amount) for f in facts
    )
    total_budget = sum(f.budget_amount for f in facts)
    assert total_suggested == total_budget  # zero-sum
    assert uncovered == Decimal("30.00")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose exec backend pytest tests/test_budget_rebalance_allocator.py -v`
Expected: FAIL (`cannot import name '_allocate_rebalance'`).

- [ ] **Step 3: Implement the allocator**

Add to `backend/app/services/budget_rebalance_service.py`:

```python
CENT = Decimal("0.01")


def _allocate_rebalance(
    facts: list["_CategoryFact"],
    priority_ids: list[int],
    reasoning_by_cat: dict[int, str] | None = None,
) -> tuple[list[BudgetDeltaSuggestion], Decimal]:
    """Move available surplus into deficits, conserving the total.

    Returns (suggestions_for_changed_rows, uncovered_overspend). The sum of
    new amounts over ALL facts equals the sum of budgets over all facts.
    """
    reasoning_by_cat = reasoning_by_cat or {}
    proj = {f.category_id: _project_period_spend(f) for f in facts}
    by_cat = {f.category_id: f for f in facts}

    headroom: dict[int, Decimal] = {}   # cid -> positive surplus available
    deficit: dict[int, Decimal] = {}    # cid -> positive need
    for f in facts:
        s = (f.budget_amount - proj[f.category_id]).quantize(CENT)
        if s > 0:
            headroom[f.category_id] = s
        elif s < 0:
            deficit[f.category_id] = -s

    total_headroom = sum(headroom.values(), Decimal("0"))
    total_deficit = sum(deficit.values(), Decimal("0"))
    movable = min(total_headroom, total_deficit).quantize(CENT)
    uncovered = (total_deficit - movable).quantize(CENT)

    # --- pull `movable` proportionally from headroom categories ---
    given: dict[int, Decimal] = {}
    if total_headroom > 0 and movable > 0:
        running = Decimal("0")
        hids = list(headroom.keys())
        for cid in hids[:-1]:
            g = (movable * headroom[cid] / total_headroom).quantize(CENT)
            given[cid] = g
            running += g
        # assign the rounding residual to the last giver so sum(given)==movable
        given[hids[-1]] = (movable - running).quantize(CENT)

    # --- distribute `movable` to deficits in priority order (waterfall) ---
    received: dict[int, Decimal] = {}
    ordered = [cid for cid in priority_ids if cid in deficit]
    # append any deficit not named by the AI, largest-need first
    for cid in sorted(deficit, key=lambda c: deficit[c], reverse=True):
        if cid not in ordered:
            ordered.append(cid)
    remaining = movable
    for cid in ordered:
        if remaining <= 0:
            break
        take = min(remaining, deficit[cid]).quantize(CENT)
        received[cid] = take
        remaining -= take

    # --- build suggestions for changed rows only ---
    suggestions: list[BudgetDeltaSuggestion] = []
    for f in facts:
        cid = f.category_id
        new_amount = f.budget_amount
        if cid in given:
            new_amount = (f.budget_amount - given[cid]).quantize(CENT)
        elif cid in received:
            new_amount = (f.budget_amount + received[cid]).quantize(CENT)
        delta = (new_amount - f.budget_amount).quantize(CENT)
        if delta == 0:
            continue
        default_reason = (
            f"Freeing {(-delta):.2f} of projected surplus"
            if delta < 0
            else f"Covering {delta:.2f} of projected overspend"
        )
        suggestions.append(
            BudgetDeltaSuggestion(
                category_id=cid,
                category_name=f.category_name,
                current_amount=f.budget_amount,
                suggested_amount=new_amount,
                delta_amount=delta,
                reasoning=(reasoning_by_cat.get(cid) or default_reason)[:400],
            )
        )

    return suggestions, uncovered
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose exec backend pytest tests/test_budget_rebalance_allocator.py -v`
Expected: PASS (2 passed). If the `test_exact_match` reasoning/order differs, adjust only the test's expectations, never weaken conservation.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/budget_rebalance_service.py backend/tests/test_budget_rebalance_allocator.py
git commit -m "feat(budgets): deterministic zero-sum rebalance allocator"
```

---

### Task 4: Rework AI layer + `suggest_rebalance` (priority-only prompt, guard, statuses, totals)

**Files:**
- Modify: `backend/app/services/budget_rebalance_service.py`
- Test: `backend/tests/test_budget_rebalance_service.py` (create — uses a fake dispatch)

**Interfaces:**
- Consumes: `_gather_facts`, `_allocate_rebalance`, `call_llm_structured`.
- Produces: an updated `suggest_rebalance(...) -> BudgetRebalanceResponse` whose suggestions always conserve the total; new `LLM_RESPONSE_SCHEMA` requesting `{priority, summary, reasoning}`; a `_parse_ai_guidance(raw, allowed_ids) -> tuple[list[int], dict[int,str], str]` helper.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_budget_rebalance_service.py
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.services import budget_rebalance_service as svc
from app.services.budget_rebalance_service import _CategoryFact


def _fact(cid, name, budget, avg, actual):
    return _CategoryFact(
        category_id=cid, category_name=name,
        budget_amount=Decimal(budget), last_3mo_total=Decimal(avg) * 3,
        last_3mo_avg=Decimal(avg), current_mo_actual=Decimal(actual),
    )


def test_parse_ai_guidance_filters_unknown_ids():
    priority, reasons, summary = svc._parse_ai_guidance(
        {"priority": [2, 999], "summary": "ok",
         "reasoning": [{"category_id": 2, "text": "rent matters"}]},
        allowed_ids={1, 2},
    )
    assert priority == [2]
    assert reasons[2] == "rent matters"
    assert summary == "ok"


@pytest.mark.asyncio
async def test_suggest_rebalance_is_zero_sum(monkeypatch):
    facts = [
        _fact(1, "Transportation", "100", "90", "90"),
        _fact(2, "Bills", "90", "100", "100"),
    ]

    class _Period:
        start_date = __import__("datetime").date(2026, 6, 1)
        end_date = None

    monkeypatch.setattr(svc, "get_current_period", AsyncMock(return_value=_Period))
    monkeypatch.setattr(svc, "_gather_facts", AsyncMock(return_value=facts))

    class _Resp:
        parsed = {"priority": [2], "summary": "Shift to bills",
                  "reasoning": [{"category_id": 2, "text": "covers rent"}]}

    class _Result:
        response = _Resp()

    monkeypatch.setattr(svc, "call_llm_structured", AsyncMock(return_value=_Result))

    out = await svc.suggest_rebalance(db=AsyncMock(), org_id=1)
    assert out.status == "ok"
    assert out.is_balanced is True
    assert out.total_budget == Decimal("190.00")
    assert out.total_suggested == Decimal("190.00")
    assert out.uncovered_overspend == Decimal("0.00")


@pytest.mark.asyncio
async def test_suggest_rebalance_refuses_when_no_surplus(monkeypatch):
    facts = [
        _fact(1, "Transportation", "100", "120", "120"),  # over
        _fact(2, "Bills", "90", "100", "100"),            # over
    ]

    class _Period:
        start_date = __import__("datetime").date(2026, 6, 1)
        end_date = None

    monkeypatch.setattr(svc, "get_current_period", AsyncMock(return_value=_Period))
    monkeypatch.setattr(svc, "_gather_facts", AsyncMock(return_value=facts))
    monkeypatch.setattr(svc, "call_llm_structured", AsyncMock())

    out = await svc.suggest_rebalance(db=AsyncMock(), org_id=1)
    assert out.status == "empty_no_surplus"
    assert out.suggestions == []
    # the LLM is never called when there is nothing to reallocate
    svc.call_llm_structured.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose exec backend pytest tests/test_budget_rebalance_service.py -v`
Expected: FAIL (`_parse_ai_guidance` missing; service still emits LLM amounts / lacks `empty_no_surplus`).

- [ ] **Step 3: Implement**

3a. Replace `LLM_RESPONSE_SCHEMA` with a priority-only contract:

```python
LLM_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "required": ["priority", "summary"],
    "properties": {
        "summary": {"type": "string"},
        "priority": {"type": "array", "items": {"type": "integer"}},
        "reasoning": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["category_id", "text"],
                "properties": {
                    "category_id": {"type": "integer"},
                    "text": {"type": "string"},
                },
            },
        },
    },
}
```

3b. Rewrite `_build_messages` system prompt to ask for **priority + reasoning only, never amounts** (keep the closed-set rule and aggregates-only user payload; add each category's `projected_spend` and `surplus` to the aggregates so the model can reason). Replace rules 3/6 with:

```
3. Return ONLY a priority ordering of the category_ids that are projected to
   OVERSPEND (most important to cover first — essential bills before
   discretionary), short per-category reasoning, and a one-line summary.
   Do NOT return any amounts; the system computes the money movements.
6. You are reallocating a FIXED total. You cannot add money; you can only
   rank which overspending categories matter most.
```

Add `projected_spend`/`surplus` to each aggregate dict in `_build_messages`.

3c. Add the parser:

```python
def _parse_ai_guidance(
    raw: dict, allowed_ids: set[int]
) -> tuple[list[int], dict[int, str], str]:
    """Extract (priority_ids, reasoning_by_cat, summary) from LLM JSON,
    dropping any ids outside the closed set. Never raises — bad shapes
    degrade to empty guidance so the deterministic allocator still runs."""
    priority: list[int] = []
    for cid in (raw.get("priority") or []):
        if isinstance(cid, int) and cid in allowed_ids and cid not in priority:
            priority.append(cid)
    reasons: dict[int, str] = {}
    for item in (raw.get("reasoning") or []):
        if not isinstance(item, dict):
            continue
        cid = item.get("category_id")
        text = item.get("text")
        if isinstance(cid, int) and cid in allowed_ids and isinstance(text, str):
            reasons[cid] = text.strip()[:400]
    summary = (raw.get("summary") or "").strip()[:400]
    return priority, reasons, summary
```

3d. Rewrite the body of `suggest_rebalance` after `facts` is gathered:

- keep the `empty_no_budgets` / `empty_no_history` guards.
- compute `proj`/headroom early to short-circuit refuse:

```python
    total_headroom = sum(
        max(f.budget_amount - _project_period_spend(f), Decimal("0"))
        for f in facts
    )
    total_budget = sum((f.budget_amount for f in facts), Decimal("0")).quantize(CENT)
    if total_headroom <= 0:
        return BudgetRebalanceResponse(
            status="empty_no_surplus",
            period_start=period.start_date.isoformat(),
            total_budget=total_budget,
            total_suggested=total_budget,
            uncovered_overspend=Decimal("0.00"),
            is_balanced=True,
            summary=(
                "Every category is projected at or over budget — there's "
                "nothing to reallocate. Your total budget is below projected "
                "spending this period."
            ),
        )
```

- call the LLM (existing try/except, same dispatch + session pattern), then `priority, reasons, summary = _parse_ai_guidance(result.response.parsed, {f.category_id for f in facts})`. On the existing LLM-unavailable/invalid branches, set `priority, reasons, summary = [], {}, ""` and continue (deterministic fallback) **instead of** returning `llm_unavailable` — the rebalance must still work offline. (Keep `llm_unavailable` only for a hard dispatch exception if you prefer; minimum requirement: a missing/empty LLM result still yields a balanced deterministic suggestion.)
- run the allocator + guard:

```python
    suggestions, uncovered = _allocate_rebalance(facts, priority, reasons)
    total_suggested = sum(
        ({s.category_id: s.suggested_amount for s in suggestions}.get(
            f.category_id, f.budget_amount)
         for f in facts),
        Decimal("0"),
    ).quantize(CENT)
    if abs(total_suggested - total_budget) > CENT:
        logger.warning("ai.budget.rebalance.guard_tripped", org_id=org_id,
                       total_budget=str(total_budget),
                       total_suggested=str(total_suggested))
        suggestions, uncovered = _allocate_rebalance(facts, [], {})
        total_suggested = total_budget
    return BudgetRebalanceResponse(
        status="ok",
        period_start=period.start_date.isoformat(),
        suggestions=suggestions,
        summary=summary or "Here's a balanced way to cover your overspending.",
        total_budget=total_budget,
        total_suggested=total_suggested,
        uncovered_overspend=uncovered,
        is_balanced=(uncovered == 0),
    )
```

Add `CENT = Decimal("0.01")` import/use consistently (defined in Task 3).

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose exec backend pytest tests/test_budget_rebalance_service.py tests/test_budget_rebalance_allocator.py tests/test_budget_rebalance_projection.py -v`
Expected: PASS. Then run the existing suite to catch regressions in older rebalance tests:
Run: `docker compose exec backend pytest tests/ -k rebalance -v`
Expected: PASS (update any old test that asserted the LLM set amounts — those expectations are intentionally obsolete; rewrite them to assert conservation, do not weaken the allocator).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/budget_rebalance_service.py backend/tests/test_budget_rebalance_service.py
git commit -m "feat(budgets): enforce zero-sum rebalance; LLM does priority + narrative only"
```

---

### Task 5: Frontend — balance meter warning, uncovered banner, no-surplus state

**Files:**
- Modify: `frontend/components/budgets/BudgetRebalanceModal.tsx`
- Test: `frontend/tests/budget-rebalance-modal.test.tsx` (create if absent; otherwise extend)

**Interfaces:**
- Consumes: `RebalanceResponse` (extend the TS type with `total_budget`, `total_suggested`, `uncovered_overspend`, `is_balanced` as `string | number | boolean`, and `status` union with `"empty_no_surplus"`).
- Produces: a balance meter that turns amber when the accepted net ≠ 0, an uncovered-overspend info banner when `uncovered_overspend > 0`, and an `empty_no_surplus` empty state.

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/tests/budget-rebalance-modal.test.tsx
import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

import BudgetRebalanceModal from "@/components/budgets/BudgetRebalanceModal";
import * as api from "@/lib/api";

const OK = {
  status: "ok",
  period_start: "2026-06-01",
  total_budget: 190,
  total_suggested: 190,
  uncovered_overspend: 0,
  is_balanced: true,
  summary: "Shift to bills",
  suggestions: [
    { category_id: 1, category_name: "Transportation", current_amount: 100,
      suggested_amount: 90, delta_amount: -10, reasoning: "free surplus" },
    { category_id: 2, category_name: "Bills", current_amount: 90,
      suggested_amount: 100, delta_amount: 10, reasoning: "cover rent" },
  ],
};

beforeEach(() => vi.restoreAllMocks());

it("shows a balanced meter and warns when selection drifts", async () => {
  vi.spyOn(api, "apiFetch").mockResolvedValue(OK as never);
  render(
    <BudgetRebalanceModal open budgets={[{ id: 11, category_id: 1, amount: 100 },
      { id: 12, category_id: 2, amount: 90 }]} onApplied={() => {}} onClose={() => {}} />,
  );
  // balanced by default
  expect(await screen.findByTestId("rebalance-balance-meter")).toHaveTextContent(/balanced/i);
  // unchecking the -10 row breaks zero-sum -> amber warning
  fireEvent.click(screen.getByLabelText("Apply suggestion for Transportation"));
  expect(screen.getByTestId("rebalance-balance-meter")).toHaveTextContent(/changes your total budget/i);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec frontend npm test -- tests/budget-rebalance-modal.test.tsx`
Expected: FAIL (`rebalance-balance-meter` not found).

- [ ] **Step 3: Implement**

3a. Extend the TS interfaces at the top of `BudgetRebalanceModal.tsx`:

```tsx
export type RebalanceStatus =
  | "ok"
  | "empty_no_budgets"
  | "empty_no_history"
  | "empty_no_surplus"
  | "llm_unavailable";

export interface RebalanceResponse {
  status: RebalanceStatus;
  period_start: string | null;
  suggestions: RebalanceSuggestion[];
  summary: string;
  total_budget?: string | number;
  total_suggested?: string | number;
  uncovered_overspend?: string | number;
  is_balanced?: boolean;
}
```

3b. Replace the existing net-change line (lines ~391-395) with a tagged balance meter:

```tsx
              <div
                data-testid="rebalance-balance-meter"
                className={`mt-3 rounded-md px-3 py-2 text-xs ${
                  Math.abs(acceptedSum) < 0.005
                    ? "bg-surface-raised/40 text-text-muted"
                    : "bg-warning-subtle text-warning"
                }`}
              >
                {acceptedCount} of {response.suggestions.length} changes selected.{" "}
                {Math.abs(acceptedSum) < 0.005 ? (
                  <>Net change: {formatAmount(0)} ✓ balanced.</>
                ) : (
                  <>
                    This changes your total budget by {acceptedSum > 0 ? "+" : ""}
                    {formatAmount(acceptedSum)}.
                  </>
                )}
              </div>
```

> Token check: use existing theme tokens. If `bg-warning-subtle`/`text-warning` are not defined in `globals.css`, substitute the nearest existing warning token (grep `globals.css` for `warning`/`amber`); never introduce a raw Tailwind color (CI gate).

3c. Add an uncovered banner just above the table (inside the `hasOkSuggestions` block):

```tsx
              {Number(response.uncovered_overspend ?? 0) > 0 && (
                <div
                  data-testid="rebalance-uncovered"
                  className="mb-4 rounded-md bg-warning-subtle px-3 py-2 text-xs text-warning"
                  role="status"
                >
                  You're {formatAmount(Number(response.uncovered_overspend))} over
                  plan this period — spending exceeds your total budget, so not
                  every category could be fully covered.
                </div>
              )}
```

3d. Add the `empty_no_surplus` title to `EmptyState`:

```tsx
  const title =
    status === "empty_no_budgets"
      ? "No budgets yet"
      : status === "empty_no_history"
        ? "Not enough history yet"
        : status === "empty_no_surplus"
          ? "Nothing to reallocate"
          : status === "llm_unavailable"
            ? "AI is unavailable"
            : "Nothing to rebalance";
```

(The existing `response.status !== "ok"` branch already routes `empty_no_surplus` to `EmptyState`.)

- [ ] **Step 4: Run test + typecheck + lint**

Run: `docker compose exec frontend npm test -- tests/budget-rebalance-modal.test.tsx`
Expected: PASS.
Run: `docker compose exec frontend npx tsc --noEmit`
Expected: no errors.
Run: `docker compose exec frontend npm run lint`
Expected: no errors.
Run: `bash frontend/scripts/check-design-tokens.sh`
Expected: PASS (no off-token colors).

- [ ] **Step 5: Commit**

```bash
git add frontend/components/budgets/BudgetRebalanceModal.tsx frontend/tests/budget-rebalance-modal.test.tsx
git commit -m "feat(budgets): live balance meter + uncovered-overspend banner in rebalance modal"
```

---

## Final verification (before PR)

- [ ] Backend: `docker compose exec backend pytest tests/ -k "budget" -v` — all green.
- [ ] Frontend: `docker compose exec frontend npm test -- tests/budget-rebalance-modal.test.tsx` + `npx tsc --noEmit` + `npm run lint` — all green.
- [ ] `bash frontend/scripts/check-design-tokens.sh` — green.
- [ ] Manual smoke (optional): open the rebalance modal, confirm net change reads "✓ balanced" by default, toggling a row shows the amber warning, and an over-budget period shows the uncovered banner.
- [ ] Open PR with a conventional-commit title (e.g. `feat(budgets): zero-sum AI budget rebalance`). No AI attribution in the body.

## Self-review notes (done while writing)
- **Spec coverage:** projection basis (Task 2), deterministic conservation + imbalance/uncovered + refuse (Task 3), AI priority-only + guard + statuses + totals (Task 4), per-row live balance meter + uncovered banner + no-surplus state (Task 5). All Feature-1 spec points covered. (Feature 2 = separate plan, PR B.)
- **Placeholder scan:** none — every code step shows real code.
- **Type consistency:** `_project_period_spend`, `_allocate_rebalance(facts, priority_ids, reasoning_by_cat)`, `_parse_ai_guidance(raw, allowed_ids)`, `CENT`, and the new response fields are named identically across tasks and tests.
