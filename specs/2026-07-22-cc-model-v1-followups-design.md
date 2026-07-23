---
name: Credit Card Model V1 — Follow-ups (utilization widget + contextual per-cycle payments)
description: 2026-07-22 owner-review follow-ups after CC Model V1 shipped. F1 = CC utilization Dashboard widget (Reports deferred). F2 = rework per-cycle payment from a standing payment_strategy config into a universal single-cycle override, offered contextually. Plus the residual _apply_match docstring fix. Architect-ruled (F1-design, F1-backend, F2).
type: project
---

# Credit Card Model V1 — Follow-ups

**Date:** 2026-07-22. **Status:** design, pre-implementation. Owner reviewed the shipped CC Model V1 (Slices 1-3 merged #566/#567/#568) locally and gave two follow-ups (F1, F2). Three architect passes folded in; two split on Reports and the coordinator adjudicated (Reports deferred). Builds on `main` (all 3 slices merged; alembic head 074).

**Pre-launch, NO backcompat obligation** (`feedback_pre_launch_state`) — enum narrowing + migration is on the table.

## Owner feedback (verbatim intent)
- **F1:** CC utilization is only a quiet text subline on the /accounts row. Want it as a **visualization** (chart) on the **Dashboard** (and Reports), able to compare multiple CCs side by side; and the forecast CC-payment more findable.
- **F2:** the per-cycle "Upcoming payments" amount entry is gated on account-level `payment_strategy ∈ {minimum_only, custom_per_period}`. That mismodels reality: full-vs-partial is a **per-month** decision (a month you can't pay the whole bill), not a standing config. Offer "pay X this cycle" **contextually near the due date**, fixed at the account level so there are no unexpected states.

---

## F1 — CC utilization Dashboard widget (Reports DEFERRED)

### Scope decision (coordinator adjudication)
The F1-backend and F1-design architects split on Reports. Ground truth: the reports engine emits one **scalar `value` per dimension group** (`QueryRow` = dims + single value; `Measure` = one `agg(field)`, aggs limited to SUM/AVG/COUNT/DISTINCT — `backend/app/reports/sources/accounts.py` `_measure_expr`). A per-account **ratio** measure (`outstanding/credit_limit`) is NOT expressible without adding a computed/ratio-measure capability to the engine + a bar-band renderer — a real M-sized backend+frontend change. **Ship the Dashboard widget now (both architects agree: frontend-only, clean); DEFER Reports** to a scoped backlog item (see "Deferred" below). This delivers the core "visualize + compare CCs" ask at low risk and keeps this a clean PR.

### Shared utilization math — `frontend/lib/credit.ts` (NEW)
Extract the utilization arithmetic currently inline in `frontend/app/accounts/page.tsx` (~lines 1287-1302) into one home so the accounts subline and the new widget can't drift:
```ts
// outstanding = max(0, -balance); util% = outstanding/limit*100 (uncapped);
// available = limit + balance; over = outstanding - limit
export function creditUtilization(balance: number, creditLimit: number): {
  outstanding: number; utilizationPct: number; available: number; over: number;
}
```
Liabilities are stored NEGATIVE. Refactor the accounts-page subline to consume this helper (no behavior change there).

### Viz form — horizontal banded bar (NOT a gauge)
Reuse the shipped `BudgetBarsWidget` idiom verbatim (`frontend/components/dashboard/widgets/BudgetBarsWidget.tsx`): a horizontal stacked bar per CC, one row per card, shared left category axis — so multiple CCs compare down a column (a gauge can't share a scale and is a reinvented affordance the app has nowhere). Over-limit reuses `BudgetSpentBarShape` (`frontend/lib/chart-shapes.tsx`): clamp the numeric domain to 100 so the fill maxes at the track; surface the overage in the text label, never let the bar exceed the track. New component `frontend/components/dashboard/widgets/CreditUtilizationBar.tsx` (or fold into the widget).

### Threshold bands (resolves the deferred owner utilization-coloring decision)
Color earned only at the risky end (quiet-by-default + One Brass Rule); every band pairs color with text (AA, color-never-alone). Token-only:

| Band | Cut | Fill token | Label |
|---|---|---|---|
| Low/Moderate | `util < 75` | `chartColor.watch` (neutral `--color-text-secondary`) | the numeric % carries it |
| High | `75 ≤ util < 100` | `var(--color-warning)` (full theme token, DESIGN.md) | "High" |
| Over | `util ≥ 100` | `chartColor.over` (`--color-danger`) | "Over limit · {over} {ccy} over" |
| Track (headroom) | — | `chartColor.remaining` (`--color-border`) | "Remaining" |

Cut logic mirrors BudgetBars' Cell-fill ternary: `util >= 100 ? over : util >= 75 ? warning : watch`. Low and Moderate deliberately share the neutral fill (distinguished by the % and fill length) — green-always-on would burn a semantic token (green = settled/actual) and violate quiet-by-default.

### Dashboard registration — new `dash_cc_utilization` widget
Mirror how `dash_budget`/`BudgetBarsWidget` is wired (frontend-only for the widget; one trivial backend validator line):
1. `frontend/lib/dashboard/widget-types.ts` — add `"dash_cc_utilization"` to `DashboardWidgetType` + a `DASHBOARD_WIDGET_DEFAULTS` entry (a `w=4, h=6`-class tile like `dash_budget`).
2. `frontend/components/dashboard/renderDashboardWidget.tsx` — add `case "dash_cc_utilization": return fill(<CreditUtilizationWidget />);`.
3. `frontend/components/dashboard/AddWidgetMenu.tsx` — add a tile entry (label "Credit card utilization", `CreditCard` lucide icon already imported).
4. NEW `frontend/components/dashboard/widgets/CreditUtilizationWidget.tsx` — reads `activeAccounts` from `useDashboard()` (like `AccountsWidget`), filters `account_type_slug === "credit_card" && Number(credit_limit) > 0`, sorts by utilization desc (highest risk on top), renders the banded bar per CC. Also renders a quiet **"Next payment {amount} on {date}"** chip (`badgeNeutral` from `lib/styles.ts`) from the account's Slice-3 `cc_payments` datum if present (F1 ruling 4 — findability without shouting; the forecast card line stays as-is).
5. **Backend (only touch):** the dashboard-layout **validator** in `backend/app/routers/dashboard.py` must ACCEPT `dash_cc_utilization` (or a saved layout containing it is rejected). Do NOT add it to `DEFAULT_DASHBOARD_LAYOUT` (users add it via the menu); keep the frontend-defaults↔backend-default parity test green.

### F1 empty/edge states
- CC with null/0 `credit_limit`: excluded from bars; if it still has a balance, a muted "No limit set" row (never divide by zero).
- No CC accounts: BudgetBars-style empty state ("No credit cards yet.") with a link to `/accounts`; widget stays addable.
- Over-limit: clamped full bar + `chartColor.over` + "Over limit · {over} {ccy} over".
- Paid-off (balance ≥ 0, limit set): 0% used, neutral track, "full limit available".

### F1 forecast-payment surfacing (ruling 4)
Keep the muted `Payment {amt} on {date}` line on `AccountMonthEndForecast` as-is (no color/size escalation — quiet-by-default). Findability comes from the "Next payment" chip in the utilization widget (above). (F2 adds a contextual "Change" affordance to that same line — see F2.)

---

## F2 — per-cycle payment: universal single-cycle override (not a standing strategy)

### Model change — collapse the enum (migration 075)
`PaymentStrategy` keeps only the two values that are legitimately **standing defaults**; the two that mismodel a per-month decision are dropped and become the *absence of a standing override, expressed per cycle*.
- **Keep:** `full_balance` (default, NULL-at-rest), `fixed_amount`.
- **Drop:** `minimum_only`, `custom_per_period`.
- `backend/app/models/account.py` — remove `MINIMUM_ONLY`, `CUSTOM_PER_PERIOD` from `PaymentStrategy`.
- **Migration 075** (`075_collapse_payment_strategy`, down_revision `074_cc_cycle_payments`):
  1. `UPDATE accounts SET payment_strategy = NULL WHERE payment_strategy IN ('minimum_only','custom_per_period');` **must run FIRST**.
  2. `ALTER TABLE accounts MODIFY COLUMN payment_strategy ENUM('full_balance','fixed_amount') NULL;` (raw value tuple, no app-model import — 045/073 idiom).
  - **VERIFY on real MySQL** (`alembic upgrade head` + downgrade + re-upgrade): SQLite CI cannot exercise `ALTER … MODIFY ENUM`. **The NULL-reset UPDATE must precede the MODIFY** or MySQL errors/truncates on out-of-set rows. This is the biggest risk.
- `fixed_payment_amount` column + its `fixed_amount` coupling: **UNCHANGED**.

### Resolver change — override-first (`cc_forecast_service.cc_target_payment`)
Check the per-cycle store FIRST for **any** CC (override wins), then the strategy default. This just removes the strategy gate on reading the store:
```
anchor = (account.id, cycle.period_end_inclusive.year, cycle.period_end_inclusive.month)
if anchor in per_cycle_amounts:      # override wins for ANY card
    return per_cycle_amounts[anchor]
if strategy == "fixed_amount":
    return fixed_payment_amount or 0
return outstanding_at_close           # full_balance / NULL default
```
Everything downstream is untouched: `capped = min(target, outstanding_at_close)` (never pays a card into credit), `P_k_owned`, `S_prev`, `outflow = max(0, capped - P_k_owned - S_prev)`. `account_balance_forecast_service` already batch-loads `per_cycle_amounts` for all CCs — no change there. **Overrides are single-cycle and never auto-carry** (call out in copy + tests).

### Validation / endpoints — near-zero change
`cc_cycle_payment_service` + `cc_cycle_payments.py` are already CC-only, NOT strategy-gated at write (Slice-2 decision). No change. `credit_card_service` validation only tests `strategy == fixed_amount`; the `else` already covers full_balance/NULL — no change beyond the enum members it imports. The only functional backend edits are the enum removal + `cc_target_payment`.

### UX — de-gate the editor section + contextual dashboard "Change" link
- **Primary (account-level, "fixed here"):** `frontend/app/accounts/page.tsx` — remove the strategy condition gating the "Upcoming payments" mini-list (show for **any** credit_card with a `close_day`; the empty-cycles branch already handles no-close-day). Remove the `minimum_only`/`custom_per_period` `<option>`s from the Payment-strategy select (only `Pay full balance` + `Pay a fixed amount` remain; default = full balance). Reframe the section copy from standing to override: helper becomes **"Paying the full balance by default. Enter a different amount for any cycle you plan to pay partially."** Keep the per-row amount input + Clear + empty-state copy.
- **Contextual (near due date):** `frontend/components/dashboard/AccountMonthEndForecast.tsx` — for the **imminent** cycle (its `due_date` within the current period / nearest upcoming), add a quiet inline **"Change"** link (`btnLink`, same as the editor's Clear) on the `Payment {amt} on {date}` line that deep-links to the account editor's cycle input for that cycle. This is where "offered as the due date approaches" lives; no new panel, no notification system (declined — contradicts quiet-by-default and adds an affordance the language lacks).
- Net: the account editor is the system of record ("fixed at the account level"); the dashboard is the contextual nudge ("near due date"). Both write the same `PUT /api/v1/accounts/{id}/cycle-payments/{year}/{month}`.

### F2 migration/data
- `cc_cycle_payments` rows: kept as-is; they silently widen into plain per-cycle overrides honored for any CC (continuity for accounts that were minimum_only/custom_per_period — their amounts keep applying after the account resets to NULL/full_balance).
- Accounts with the dropped strategies: migration 075 resets them to NULL (= full_balance default). Lossless in intent (the amounts survive in `cc_cycle_payments` as overrides).

---

## Residual — `_apply_match` docstring correction
`backend/app/models/transaction.py` docstring claims `linked_transaction_id` is "created only by `_link_pair` … other code paths must not write this column directly," but `reconciliation_service._apply_match` writes it one-way for reconcile matches (the ambiguity that nearly caused the Slice-3 forecast bug, now handled by `balance_contribution_filter`). Correct the docstring to accurately describe both writers (bidirectional via `_link_pair` for transfers/pairing; one-way via `_apply_match` for reconciliation matches), so the next reader isn't misled. Docstring-only; no behavior change. (The deeper "should matches overload this column?" refactor stays a separate backlog item.)

---

## Deferred (backlog, NOT in this PR)
- **F1 Reports utilization** — surface per-account utilization in Reports. Requires extending the reports engine with a computed/ratio measure (the agg framework is SUM/AVG/COUNT only) + a bar-band renderer routing over-limit through `chartColor.over`. Integration point: widen `MeasureField` (`backend/app/schemas/reports_enums.py`), add `credit_limit`/`utilization` measures + `build_rows` projection to `backend/app/reports/sources/accounts.py`, band-color the `BarWidgetChart` when the measure is `utilization`. **M** backend+frontend, decision-gated (ratio-measure semantics, cross-currency aggregate caveat). Sibling: NetWorth Phase 6.

## Out of scope (reaffirmed)
No new endpoints for the dashboard widget (reuse `useAccounts`/`activeAccounts`); no changes to the cc_cycle_payments store/endpoints; no changes to the Slice-3 forecast machinery beyond the `cc_target_payment` override-first branch; no reinvented affordances (gauge, notification prompts).

## Sequencing (one branch `cc-model-v1-followups`, one PR; ordered)
1. **F2 backend:** migration 075 (verify on MySQL) + enum removal + `cc_target_payment` override-first + tests.
2. **F2 frontend:** de-gate the editor "Upcoming payments" + strategy-select option removal + copy reframe; dashboard forecast "Change" link + tests.
3. **F1:** `lib/credit.ts` extraction (+ refactor accounts subline) → `CreditUtilizationBar` → `CreditUtilizationWidget` (+ next-payment chip) → registry (`widget-types`/`renderDashboardWidget`/`AddWidgetMenu`) → backend layout-validator accepts `dash_cc_utilization` + tests.
4. **Residual:** `transaction.py` docstring fix.

Each task: TDD, run in the isolated `-p team-ccm1` stack; frontend gates = tsc + eslint --quiet + design-token check; migration verified on MySQL. Whole-branch review before the single PR. No AI attribution.

## Cross-references
- `reference_cc_model_v1` (memory) — the shipped 3 slices + the `balance_contribution_filter` gotcha this residual documents.
- `specs/credit-card-model-upgrade.md`, `specs/2026-05-28-cc-billing-cycle.md`, `specs/2026-07-22-cc-model-v1-design.md` — CC Model V1 sources.
- `specs/2026-06-13-reports-v3-phase5-accounts-source-design.md` — AccountsSource pattern for the deferred Reports work.
