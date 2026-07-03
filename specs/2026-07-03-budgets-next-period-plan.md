# Next-Period Budgeting (PR B) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Let the user budget the *next* billing period alongside the current one, seeding an empty next period via four paths (from-forecast, copy-forward, AI-draft-from-trends, blank).

**Architecture:** Backend gains three period-aware seed endpoints on the existing `/api/v1/budgets` router; all reuse `resolve_period` + the per-row savepoint idempotency already used by `create_budgets_from_forecast`. Because `resolve_period` raises if the next-period `BillingPeriod` stub doesn't exist, every next-period write first calls `ensure_future_periods` to materialize the stub. Frontend adds a "This period / Next period" selector, surfaces the future stub (relaxing the `start_date <= today` filter), ungates budget mutations for the next period, and offers the four seed actions in the empty state.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.0 async / Pydantic v2 (backend); Next.js 16 / React 19 / TypeScript / Vitest (frontend).

## Global Constraints

- Design doc (locked): `specs/2026-06-24-ai-budget-rebalance-zerosum-and-next-period-design.md` §"Feature 2".
- Org-scoped queries throughout; auth via `get_current_user`.
- Money is `Decimal`, quantized to `Decimal("0.01")`.
- Budget uniqueness: `uq_budget_org_cat_period` on `(org_id, category_id, period_start)` — seeds skip-existing via per-row `db.begin_nested()` swallowing `IntegrityError` (idempotent), mirroring `create_budgets_from_forecast`.
- Frontend: token classes only (No-Off-Token, CI-gated by `frontend/scripts/check-design-tokens.sh`); One Brass Rule / Sidebar-Always-Navy / WCAG 2.2 AA hold. No em-dashes in customer copy.
- Verify set MUST include `npm run lint` (eslint `no-explicit-any`/`no-unescaped-entities` are CI-gated, not covered by tsc).
- No AI attribution in commits or PR body.
- **Scope note:** only "current" + "next" (no multi-period); the period/billing model is unchanged; seeds are advisory (nothing auto-applied).

## ⚠️ Two decision points flagged for the operator (before Task 3 + frontend)

1. **AI-draft: LLM or pure projection?** The locked design says "AI-draft" routed via `call_llm_structured` behind the `ai.budget` entitlement, but ALSO says the draft is "pure projection (no actuals exist next period)." Task 3 below is written the faithful way (deterministic projection amounts + offline-resilient LLM narrative, gated behind `ai.budget`, mirroring the just-shipped rebalance service). If you'd rather ship a leaner deterministic-only draft (no dispatch, no entitlement gate), say so and I'll trim Task 3.
2. **AI-draft "apply" is a CREATE, not an UPDATE.** The rebalance modal applies by `PUT`-ing existing budget ids; a next-period draft has no budget rows yet, so "apply" must `POST` create per accepted category. Task 6 builds this as a distinct apply path. Confirm that UX (a dedicated next-period draft review, reusing the suggestion table but creating on apply) before I build the frontend.

**Execution order:** Tasks 1–2 are unambiguous and decision-free (execute now). Tasks 3–6 wait on the two decisions above.

**DECISIONS (operator, 2026-07-03):** (1) **Deterministic-only draft** — no LLM, not behind `ai.budget`; projected amounts + a deterministic reasoning string. (2) **Dedicated draft review modal** — reuse the suggestion-table layout, NO balance meter, Apply `POST`s `create_budget` per accepted category.

---

### Task 1: Period-aware `from-forecast`

**Files:**
- Modify: `backend/app/services/budget_service.py` (`create_budgets_from_forecast`)
- Modify: `backend/app/routers/budgets.py` (`create_from_forecast` route)
- Test: `backend/tests/services/test_budgets_next_period.py` (create)

**Interfaces:**
- Produces: `create_budgets_from_forecast(db, org_id, period_start: date | None = None) -> list[BudgetResponse]` — defaults to current period (back-compat); otherwise ensures the future stub exists, resolves the named period, and seeds from that period's forecast plan (idempotent, skip-existing).
- Route: `POST /api/v1/budgets/from-forecast?period_start=YYYY-MM-DD` (param optional).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/services/test_budgets_next_period.py
# (reuses the in-memory-sqlite fixtures pattern from
#  tests/services/test_budget_rebalance.py: session_factory, db, org, user,
#  period, categories. Copy those fixtures or import a shared conftest.)
import datetime
from decimal import Decimal

import pytest

from app.services import budget_service
from app.services.billing_service import ensure_future_periods, get_current_period


@pytest.mark.asyncio
async def test_from_forecast_seeds_named_next_period(
    db, org, user, period, categories
):
    # Materialize the next-period stub and its forecast plan, then seed.
    await ensure_future_periods(db, org_id=org.id)
    next_start = await _next_period_start(db, org.id)  # helper in this file
    await _seed_forecast_plan(  # helper: one EXPENSE item, $250, groceries
        db, org_id=org.id, period_start=next_start,
        category_id=categories["groceries"].id, amount=Decimal("250.00"),
    )

    out = await budget_service.create_budgets_from_forecast(
        db, org_id=org.id, period_start=next_start
    )
    assert any(
        b.category_id == categories["groceries"].id
        and b.amount == Decimal("250.00")
        and b.period_start == next_start
        for b in out
    )
    # current period is untouched
    current = await budget_service.list_budgets(db, org_id=org.id)
    assert current == []
```

> Implementer note: write the two small helpers `_next_period_start` (query the earliest `BillingPeriod.start_date > current.start_date`) and `_seed_forecast_plan` (create a `ForecastPlan` for the target `BillingPeriod.id` with one `ForecastItem` of type EXPENSE) at the top of the test module. Mirror the ForecastPlan/ForecastItem construction in `tests/**` that already exercises `create_budgets_from_forecast`.

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec -T backend pytest tests/services/test_budgets_next_period.py::test_from_forecast_seeds_named_next_period -v`
Expected: FAIL (`create_budgets_from_forecast() got an unexpected keyword argument 'period_start'`).

- [ ] **Step 3: Implement**

In `budget_service.py`, change the signature and period resolution:

```python
async def create_budgets_from_forecast(
    db: AsyncSession, org_id: int, period_start: date | None = None
) -> list[BudgetResponse]:
    # Ensure the next-period stub exists so resolve_period can find it.
    await ensure_future_periods(db, org_id=org_id)
    period = await resolve_period(db, org_id, period_start)
    # ... rest unchanged: find ForecastPlan WHERE billing_period_id == period.id,
    #     skip-existing via _get_existing_budget_cat_ids(db, org_id, period.start_date),
    #     per-row savepoint create, return list_budgets(db, org_id, period_start=period.start_date)
```

Add `from app.services.billing_service import ensure_future_periods` if not already imported. In `routers/budgets.py`, add the query param:

```python
@router.post("/from-forecast", response_model=list[BudgetResponse])
async def create_from_forecast(
    period_start: date | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await budget_service.create_budgets_from_forecast(
        db, user.org_id, period_start=period_start
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose exec -T backend pytest tests/ -k "budget" -q`
Expected: PASS (new test + all existing budget tests; the current-period back-compat path is unchanged).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/budget_service.py backend/app/routers/budgets.py backend/tests/services/test_budgets_next_period.py
git commit -m "feat(budgets): period-aware from-forecast seed for next period"
```

---

### Task 2: `copy-from-period` seed

**Files:**
- Modify: `backend/app/services/budget_service.py` (new `copy_budgets_from_period`)
- Modify: `backend/app/schemas/budget.py` (new `CopyBudgetsRequest`)
- Modify: `backend/app/routers/budgets.py` (new route)
- Test: `backend/tests/services/test_budgets_next_period.py` (extend)

**Interfaces:**
- Consumes: `resolve_period`, `ensure_future_periods`, `_get_existing_budget_cat_ids`, `list_budgets`.
- Produces: `copy_budgets_from_period(db, org_id, *, source_period_start: date, target_period_start: date | None = None) -> list[BudgetResponse]` — bulk-creates target budgets from source amounts, skipping categories already budgeted in target (idempotent). Raises `ValidationError("Source period has no budgets to copy")` when source is empty.
- Schema: `CopyBudgetsRequest{source_period_start: date, target_period_start: date | None = None}`.
- Route: `POST /api/v1/budgets/copy-from-period` (body).

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_copy_from_period_seeds_target_and_is_idempotent(
    db, org, user, period, categories
):
    # Seed current-period budgets, then copy forward to next period.
    await budget_service.create_budget(
        db, org_id=org.id,
        body=BudgetCreate(category_id=categories["groceries"].id, amount=Decimal("400.00")),
    )
    await budget_service.create_budget(
        db, org_id=org.id,
        body=BudgetCreate(category_id=categories["dining"].id, amount=Decimal("200.00")),
    )
    await ensure_future_periods(db, org_id=org.id)
    next_start = await _next_period_start(db, org.id)

    out = await budget_service.copy_budgets_from_period(
        db, org_id=org.id, source_period_start=period.start_date,
        target_period_start=next_start,
    )
    amounts = {b.category_id: b.amount for b in out}
    assert amounts[categories["groceries"].id] == Decimal("400.00")
    assert amounts[categories["dining"].id] == Decimal("200.00")
    assert all(b.period_start == next_start for b in out)

    # Idempotent: second copy is a no-op (no duplicate rows / no error).
    again = await budget_service.copy_budgets_from_period(
        db, org_id=org.id, source_period_start=period.start_date,
        target_period_start=next_start,
    )
    assert len(again) == 2


@pytest.mark.asyncio
async def test_copy_from_empty_source_raises(db, org, user, period, categories):
    from app.core.errors import ValidationError  # adjust import to the real path
    await ensure_future_periods(db, org_id=org.id)
    next_start = await _next_period_start(db, org.id)
    with pytest.raises(ValidationError):
        await budget_service.copy_budgets_from_period(
            db, org_id=org.id, source_period_start=period.start_date,
            target_period_start=next_start,
        )
```

> Implementer note: confirm the real `ValidationError` import path used by `budget_service` (grep the top of `budget_service.py`) and the `BudgetCreate` import; adjust the test imports to match.

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose exec -T backend pytest tests/services/test_budgets_next_period.py -k copy -v`
Expected: FAIL (`copy_budgets_from_period` missing).

- [ ] **Step 3: Implement**

Schema in `schemas/budget.py`:

```python
class CopyBudgetsRequest(BaseModel):
    source_period_start: date
    target_period_start: date | None = None
```

Service in `budget_service.py` (mirror `create_budgets_from_forecast`'s savepoint idiom):

```python
async def copy_budgets_from_period(
    db: AsyncSession,
    org_id: int,
    *,
    source_period_start: date,
    target_period_start: date | None = None,
) -> list[BudgetResponse]:
    await ensure_future_periods(db, org_id=org_id)
    target = await resolve_period(db, org_id, target_period_start)
    source = await resolve_period(db, org_id, source_period_start)

    source_rows = (
        await db.execute(
            select(Budget).where(
                Budget.org_id == org_id,
                Budget.period_start == source.start_date,
            )
        )
    ).scalars().all()
    if not source_rows:
        raise ValidationError("Source period has no budgets to copy")

    existing = await _get_existing_budget_cat_ids(db, org_id, target.start_date)
    for row in source_rows:
        if row.category_id in existing:
            continue
        try:
            async with db.begin_nested():
                db.add(
                    Budget(
                        org_id=org_id,
                        category_id=row.category_id,
                        amount=row.amount,
                        period_start=target.start_date,
                        period_end=target.end_date,
                    )
                )
        except IntegrityError:
            # Concurrent insert hit uq_budget_org_cat_period — already seeded.
            pass
    await db.commit()
    return await list_budgets(db, org_id, period_start=target.start_date)
```

Route in `routers/budgets.py`:

```python
@router.post("/copy-from-period", response_model=list[BudgetResponse])
async def copy_from_period(
    body: CopyBudgetsRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await budget_service.copy_budgets_from_period(
        db, user.org_id,
        source_period_start=body.source_period_start,
        target_period_start=body.target_period_start,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose exec -T backend pytest tests/ -k "budget" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/budget_service.py backend/app/schemas/budget.py backend/app/routers/budgets.py backend/tests/services/test_budgets_next_period.py
git commit -m "feat(budgets): copy-from-period seed for next period"
```

---

### Task 3: AI next-period draft  *(GATED on decision #1)*

**Files:**
- Create: `backend/app/services/budget_draft_service.py`
- Modify: `backend/app/routers/` (budgets router or a dedicated ai route — match the rebalance route location)
- Test: `backend/tests/services/test_budget_draft.py` (create)

**Interfaces:**
- Consumes: `_project_period_spend`, `_CategoryFact` (from `budget_rebalance_service`); `call_llm_structured` (offline-resilient, narrative-only); `ensure_future_periods`, `resolve_period`.
- Produces: `suggest_next_period_budget(db, org_id, *, period_start, session_factory=None) -> BudgetRebalanceResponse` — one `BudgetDeltaSuggestion` per expense category with 3-month history, `current_amount=0`, `suggested_amount=projected` (= `last_3mo_avg`, since next-period actuals are 0), `delta_amount=projected`. Amounts are deterministic; the LLM supplies summary + per-category reasoning and degrades gracefully offline.
- Route: `POST /api/v1/budgets/draft-next?period_start=...` behind the `ai.budget` entitlement.

Detailed steps deferred until decision #1 is resolved (LLM vs deterministic-only). The projection window for a next period is `[period_start - 3 months, period_start)` with `current_mo_actual = 0`; only categories with `last_3mo_avg > 0` are drafted.

---

### Task 4: Frontend — surface the next period  *(GATED on decision #2)*

**Files:**
- Modify: `frontend/app/budgets/page.tsx`

Relax `loadRefs`' `start_date <= todayISO()` filter to include the single next stub; add an `isNextPeriod` flag (`selectedPeriod.start_date > todayISO() && end_date != null` for the nearest future stub); keep past periods read-only. Period nav already exists; the selector just needs the next stub in `periods`.

---

### Task 5: Frontend — empty next-period state + four seed actions  *(GATED)*

**Files:**
- Modify: `frontend/app/budgets/page.tsx`

Empty-next-period state with four buttons: From forecast (`POST /from-forecast?period_start=`), Copy this period forward (`POST /copy-from-period`), AI draft from trends (Task 6), Start blank (dismiss to the ungated add-budget form). Ungate `+ Add Budget` for `isNextPeriod` (the create form already passes `period_start`).

---

### Task 6: Frontend — AI-draft review + create-on-apply  *(GATED on decision #2)*

**Files:**
- Create: `frontend/components/budgets/BudgetDraftModal.tsx` (or extend the rebalance modal with a create-apply mode)

Fetches `POST /draft-next?period_start=`, shows the suggestion table (reusing the rebalance row layout), and on Apply `POST`s `create_budget?period_start=` per accepted category (create, not update). No balance meter (a draft is not conservation-constrained).

---

## Final verification (before PR)

- [ ] Backend: `docker compose exec -T backend pytest tests/ -k "budget" -v` — all green.
- [ ] Frontend: budgets page tests + `npx tsc --noEmit` + `npm run lint` + `bash frontend/scripts/check-design-tokens.sh` — all green.
- [ ] Open PR with a conventional-commit title (e.g. `feat(budgets): next-period budgeting`). No AI attribution.
