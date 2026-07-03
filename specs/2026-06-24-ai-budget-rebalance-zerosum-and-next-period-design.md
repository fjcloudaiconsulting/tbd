# AI Budget Rebalance (zero-sum) + Next-Period Budgeting — Design

**Date:** 2026-06-24
**Status:** Approved (brainstorm complete; user waived the spec-review gate)
**Author context:** Operator-reported behavior + idea. Two related budget-domain features.

## Motivation

Two operator asks:

1. **Rebalance must be zero-sum.** Today's AI budget rebalance can inflate or deflate the *total* budget — it invents/removes money rather than moving it between categories. The operator wants: if Transportation is budgeted 100 but projected to spend 90, and Bills is budgeted 90 but projected to spend 100, the rebalance should **move 10 from Transportation to Bills** — conserving the total. Otherwise the budget "becomes variable" and loses meaning.

2. **Budget the next period.** Mirroring Forecast Plans (which already let you plan next month), let the user budget *next* month's expenses alongside the forecast.

## Current state (grounding)

- `backend/app/services/budget_rebalance_service.py` (LAI.3) already exists. It aggregates per-master-category facts (`budget_amount`, `last_3mo_avg`, `current_mo_actual`) and asks the LLM for per-category **absolute new amounts**.
- **The conservation rule is only a soft prompt instruction** (system-prompt rule #6: "sum should stay close to current budgets within 10% — reallocate, do not inflate"). `_validate_and_shape()` enforces only: category-id in the allowed set, non-negative, numeric. **It never checks the total is conserved.** This is the root cause of the operator's complaint.
- `BudgetRebalanceModal.tsx` shows per-category suggested deltas with per-row accept; apply reuses `PUT /api/v1/budgets/{id}`.
- Budgets are period-scoped: `GET/POST /api/v1/budgets` already accept a `period_start` query param. Future-period budgets are **storable today**.
- `POST /api/v1/budgets/from-forecast` exists but is **hardcoded to the current period** (`create_budgets_from_forecast(db, org_id)` — no period arg). It is idempotent (skips categories that already have a budget).
- `POST /api/v1/budgets/transfer` exists (move an `amount` from `from_budget_id` to `to_category_id`).
- Forecast Plans (`forecast_plans` router/service) model "next period" via a `period_start` query param on every endpoint and have `copy_from_period(source, target)`.

## Locked decisions (from brainstorm)

### Feature 1 — Zero-sum (hybrid) rebalance
1. **Hybrid logic:** deterministic surplus-shuffle base + AI trend/priority refinement + a **hard zero-sum guard enforced in Python** (not the prompt).
2. **Basis = projected end-of-period spend**, not raw spent-so-far. Use the trend data the service already gathers to project each category's full-period spend; `surplus_i = budget_i − projected_i`.
3. **Imbalance handling:** redistribute as much of the deficit as available surplus allows (AI prioritizes *which* deficits get covered first); report the **uncovered gap** honestly; **refuse** (empty suggestion + clear message) only when redistribution is genuinely impossible (no surplus anywhere to draw from).
4. **Apply model:** keep **per-row accept/skip**, but add a **live balance meter** that recomputes the net change as rows toggle and warns (amber) when the accepted set drifts from zero. The AI suggestion is always zero-sum; the user may deviate but never silently.

### Feature 2 — Next-period budgeting
5. **Period selector** on the Budgets page (This period / Next period), mirroring Forecast Plans. Data layer already supports `period_start`.
6. **Four seed paths** for an empty next period (all four wanted):
   - **From forecast plan** — `from-forecast` pointed at the next period.
   - **Copy current forward** — duplicate this period's budgets into next.
   - **AI-draft from trends** — propose next-period budgets from projected spend (no actuals exist next period, so this is pure projection).
   - **Blank / manual** — switch period and add by hand.

---

## Feature 1 design — Zero-sum rebalance

### Architecture: guarantee in Python, judgment in the LLM

**Math layer (deterministic, in `budget_rebalance_service.py`):**
1. Extend `_gather_facts` (or a new step) to compute a **projected full-period spend** per category from existing trend inputs. Projection rule (deterministic, documented):
   `projected_i = max(current_mo_actual_i, last_3mo_avg_i)` — conservative: a category that is already pacing above its 3-month average is projected at its higher run-rate, so we never free money a category is on track to need. (Simple, explainable, no extra queries.)
2. `surplus_i = budget_i − projected_i`. Split into `headroom` (surplus > 0) and `deficit` (surplus < 0).
3. **Deterministic allocator:** `movable = min(sum(headroom), sum(|deficit|))`. Pull `movable` proportionally from headroom categories; distribute to deficit categories in an AI-provided priority order (default: largest deficit first). The resulting per-category `suggested_amount` set **conserves the total by construction**.
4. Compute `uncovered_overspend = max(0, sum(|deficit|) − sum(headroom))`.
5. **Refuse condition:** `sum(headroom) == 0` → return `status="empty_no_surplus"` with a message ("Every category is projected at or over budget — there's nothing to reallocate; your total budget is below projected spending").

**AI layer (judgment + voice only):**
- The LLM receives the per-category facts **plus the computed projection/surplus** and returns: (a) a **priority ordering** of deficit categories (which to cover first — essentials before discretionary), and (b) per-move `reasoning` + a `summary`. It does **not** set the totals.
- The deterministic allocator consumes the AI priority order. If the LLM is unavailable/invalid, fall back to **default priority (largest deficit first)** — the rebalance still works without AI (graceful degrade), it just loses the prioritization nuance and custom narrative.

**Hard guard:** after shaping, assert `abs(sum(suggested_amount) − sum(current budget)) <= 0.01`. If violated (should be impossible given the deterministic allocator, but defends against refactors), drop back to the deterministic baseline and log `ai.budget.rebalance.guard_tripped`.

### Schema changes (`schemas/budget_rebalance.py`)
Add to `BudgetRebalanceResponse`:
- `total_budget: Decimal`
- `total_suggested: Decimal` (== total_budget when balanced)
- `uncovered_overspend: Decimal`
- `is_balanced: bool`
- new `status` value `empty_no_surplus`.

`BudgetDeltaSuggestion` is unchanged (already carries `current_amount`, `suggested_amount`, `delta_amount`, `reasoning`).

### Frontend (`BudgetRebalanceModal.tsx`)
- Render each suggested move as `current → suggested` with a signed, colored delta.
- **Balance meter** (sticky header/footer): "Net change: €0.00 ✓ balanced" computed from the *accepted* rows; turns amber with "This changes your total budget by +€X.XX" when the accepted set nets non-zero.
- Show `uncovered_overspend` as an info banner when > 0 ("You're €X over plan this period — spending exceeds your total budget").
- Apply unchanged: `PUT /api/v1/budgets/{id}` per accepted row.

### Testing
- Unit: allocator conserves total; imbalance leaves correct `uncovered_overspend`; refuse path on zero headroom; guard fallback on forced AI drift; AI-unavailable → default-priority deterministic result.
- Frontend: balance meter math on toggle; amber drift warning; uncovered banner.

---

## Feature 2 design — Next-period budgeting

### Backend
- **`from-forecast` gains `period_start`** (`POST /api/v1/budgets/from-forecast?period_start=...`); `create_budgets_from_forecast(db, org_id, period_start=None)` defaults to current (back-compat) and otherwise seeds the given period from that period's forecast plan. Stays idempotent.
- **New `POST /api/v1/budgets/copy-from-period`** (`{source_period_start, target_period_start}`) — mirrors Forecast Plans' `copy_from_period`; bulk-creates budgets in the target period from the source, skipping categories already budgeted in the target (idempotent).
- **AI next-period draft:** extract the projection logic from the rebalance service into a shared helper (`_project_period_spend`), then add a **dedicated `suggest_next_period_budget(db, org_id, period_start)`** service + `POST /api/v1/budgets/draft-next?period_start=...` route. Since the next period has no actuals, the draft is `suggested_amount = projected_i` per category (projection from the shared helper). It returns the same `BudgetDeltaSuggestion` shape so the frontend reuses the suggestion-apply UI. (Chosen over a `mode` flag on the rebalance service because rebalance is conservation-constrained and current-period-only; a draft has neither constraint — keeping them separate avoids overloading one function with two contracts.)

### Frontend (Budgets page)
- **Period selector** (segmented "This period" / "Next period"); switching sets the `period_start` used by all budget calls.
- **Empty next-period state** offering the four seed actions (forecast / copy-forward / AI-draft / blank). Once seeded, the page behaves identically to the current-period budgets view.
- Rebalance (Feature 1) is **current-period only** (it needs actuals); the next period's AI help is the *draft* path, which is projection-only.

### Testing
- Backend: `from-forecast` targets the named period; `copy-from-period` idempotency + skip-existing; next-period draft returns projected amounts.
- Frontend: period switch refetches; empty-state seed actions; seeded next period edits/saves.

---

## Phasing (two PRs)
- **PR A — Feature 1 (zero-sum rebalance).** Self-contained: service rework + schema + modal + tests. Ships independently and fixes the reported bug.
- **PR B — Feature 2 (next-period budgeting).** Period selector + four seed paths + endpoint additions + tests. Depends on nothing in PR A but naturally follows.

## Design-system / project constraints that hold
- **AI dispatch:** route via `call_llm_structured` with `ROUTING_KEY="smart_budget"`; cap/ledger via the existing dispatcher; run dispatch in its own session (existing pattern). The projected-overspend cap gate at dispatch is unchanged.
- **Aggregates-only prompt** (no raw transactions/PII) — preserved.
- **No Off-Token / One Brass / Sidebar-Navy / WCAG 2.2 AA** — modal + period selector use token classes only; verify with `check-design-tokens.sh`.
- **Verify set includes `npm run lint`** (eslint `no-explicit-any` is CI-gated, not covered by tsc).
- **No AI attribution** in commits or PR bodies.
- **Org-scoped** queries throughout; auth via `get_current_user`; `ai.budget` entitlement gate stays in the router.

## Out of scope
- Auto-applying rebalances (always advisory).
- Multi-period budgeting beyond "next" (only current + next).
- Changing the period/billing model.
- Rebalancing the next period off actuals (next period has none — draft is projection-only).
