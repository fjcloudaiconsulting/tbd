# Transaction Effective-Date Consistency (cash-basis everywhere + dual-date display)

**Date:** 2026-06-15
**Status:** design approved (operator), pre-spec-review
**Trigger:** A transaction created in May but settled in June ("GBLT") appears in the June *transactions list* (which buckets by settled date) but is **missing from the June report** (which buckets by raw `date`). The two surfaces answer "which period?" differently. Operator: make it consistent (cash-basis: count a transaction in the period it settled) AND make both dates visible wherever a transaction is shown — "we can't hide such information."

## Decision (operator-locked)
- **Approach B (non-destructive):** keep `date` + `settled_date` separate; standardize every period-bucketing/reporting surface on the EXISTING `effective_period_date_expr()` = `coalesce(settled_date, date)`. Do NOT mutate `date`. (Approach A — overwrite `date := settled_date` on settle — was rejected as destructive: it loses the original occurrence date and unwinds the deliberate date/settled_date split.)
- **Dual-date display everywhere a transaction is visible.** Surface BOTH the original date and the settled date on every transaction-display surface, so a May-created/June-settled row is self-explanatory in a June view. Hiding the settled date is what made the current behavior feel "fundamentally wrong."
- **App-wide cash-basis is deliberate.** Document it so a later change doesn't "fix" a forecast back to raw `date`.

## Audit — which date each surface uses today

**Already bucket by effective/settled date (no change, or minor alignment):**
- Transactions list — date filter + sort (`transaction_service.py:1995/1997/2108`, `effective_period_date_expr()`).
- Dashboard account-balance month-end forecast (`account_balance_forecast_service.py:78`).
- **Budgets** — `budget_service.py:47,52` + `budget_rebalance_service.py:203-219` already use `Transaction.settled_date` directly. **Minor alignment:** switch these to `effective_period_date_expr()` so pending-with-estimate and the date-fallback behave identically to every other surface (today a pending row with a NULL settled_date silently drops from the budget period). Behavior-identical for settled rows.

**Laggards using raw `Transaction.date` (the bug) — switch to `effective_period_date_expr()`:**
- **Reports** (`reports_query_service.py`): the month/week/day time-dimension expressions (`:98-110`) AND the `DATE` filter column (`_FILTER_COLUMN[FilterField.DATE]`, `:144`).
- **forecast_service.py** (`:96-108, :165-166`).
- **forecast_plan_service.py** (`:531-573`).
- **scenario_engine.py** (`:668-669`).

**Scope guard — do NOT change (these use the literal calendar date on purpose, not the financial period):**
- `recurring_service.py` — due-date matching/generation (`:158,:228,:301`).
- `import_service.py` — dedup window (`:163`).
- `transaction_suggestions_service.py` — "last used" (`:131,:158`).
Each remaining `Transaction.date` site will be classified in the plan (change vs leave) with a one-line rationale; nothing changes without that classification.

## Frontend — dual-date display on EVERY transaction-display surface
Add a **Settled date** alongside the existing date wherever a transaction renders. The API already returns `settled_date` on `TransactionResponse`, so this is display-only.
- **Transactions list** (`app/transactions/page.tsx` + its row/table component): keep the existing column for `date` (the original/occurrence date), add a **Settled date** column. The list already filters/sorts by the effective date; now the Settled column shows *why* a row is in a given period. For a pending row, show the expected-settlement estimate or "—".
- **Dashboard "recent transactions"** (`app/dashboard/page.tsx`): show both dates (compact form acceptable — e.g. settled date primary with the original as secondary/tooltip if space-constrained, but it must be visible, not hidden).
- **Account detail recent transactions**, **transaction detail/edit** (`TransactionForm.tsx`), **batch view**, **transfer modals** — wherever a transaction's date is shown, show the settled date too. The plan will enumerate the exact components from a frontend audit.
- **CSV export** (transactions export): add a `settled_date` column.
- **Column labels:** operator term is "Creation date" / "Settled date". Technically `date` is the transaction/purchase date you assign (not the DB row-creation timestamp). Final labels (e.g. "Date" / "Settled", or "Booked" / "Settled") are cosmetic — lock in the plan; both columns must be present and clearly distinct.

## Components / data flow
- One backend change of substance: swap `Transaction.date` → `effective_period_date_expr()` at the classified bucketing sites (reports, the 3 forecast services, budgets-alignment). The expression already exists and is SQLite/MySQL-portable (it's `coalesce`).
- Frontend: purely additive display of an already-returned field across the enumerated surfaces + the CSV column.
- No model change, **no migration**.

## Testing
- **Backend:** for each switched surface, a test proving a transaction whose `date` and `settled_date` fall in DIFFERENT months is bucketed by the SETTLED month (the GBLT case): reports time-grouping + DATE filter; forecast_service; forecast_plan_service; scenario_engine; budgets (settled month, plus a pending-with-estimate row now included via the fallback). Confirm the scope-guarded sites (recurring due-date, import dedup, suggestions) are UNCHANGED (their existing tests stay green).
- **Frontend:** each transaction-display surface renders both dates; the list shows a Settled column; pending rows render the estimate/"—"; CSV includes `settled_date`. Full `eslint . --quiet` + `tsc --noEmit` + `vitest run`.
- Cross-surface consistency check: the GBLT-style fixture appears in the same period in the list, the report, and the relevant forecast.

## Process
Subagent-driven (backend bucketing phase, then frontend display phase), review gate per phase, then a fleet review before the PR. Backend tests in an isolated `-p team-*` stack. The plan will (1) complete the frontend transaction-display-surface enumeration and (2) classify every remaining `Transaction.date` site.
