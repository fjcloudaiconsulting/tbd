# Transaction Effective-Date Consistency — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make every period-bucketing/reporting surface count a transaction in the period it *settled* (cash-basis), using the existing `effective_period_date_expr()`, and display both the original **Date** and the **Settled** date wherever a transaction renders.

**Architecture:** Backend — swap raw `Transaction.date` → `effective_period_date_expr()` (= `coalesce(settled_date, date)`) at the classified bucketing sites only (Reports, the 3 forecast services, budget alignment). Frontend — additive display of the already-returned `settled_date` on every transaction-render surface. No model change, no migration.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.0 async (backend); Next.js/React/TS/Vitest (frontend).

**Reference spec:** `specs/2026-06-15-transaction-effective-date-consistency-design.md`

**Backend tests:** isolated stack only — `docker compose -p team-txdate up -d backend mysql redis`, then `-p team-txdate` on every exec. NEVER the default `pfv` stack; NEVER `./pfv migrate`.

---

## Shared test helper (used by every backend task)
Each backend task asserts the **GBLT case**: a transaction whose `date` and `settled_date` fall in DIFFERENT months is bucketed by the **settled** month. Seed pattern (adapt to each test file's existing fixtures):
```python
# created/dated in May, settled in June
tx = Transaction(org_id=org.id, account_id=acct.id, category_id=cat.id,
                 description="GBLT", amount=Decimal("459.68"), type=TransactionType.EXPENSE,
                 status=TransactionStatus.SETTLED, date=date(2026,5,31), settled_date=date(2026,6,15))
```
Reportable filter still applies (not a transfer leg / manual adjustment).

---

## Phase 1 — Backend: cash-basis bucketing everywhere

### Task 1: Reports time-dimension bucketing (month/week/day)
**Files:** Modify `backend/app/services/reports_query_service.py` (`_dimension_expr`, the MONTH/WEEK/DAY branches, ~`:96-110`). Test: `backend/tests/services/test_reports_query_service.py`.

- [ ] **Step 1: Failing test** — group a GBLT-style settled-in-June expense by `month`; assert it lands in `2026-06`, not `2026-05`.
```python
@pytest.mark.asyncio
async def test_month_bucketing_uses_settled_date(db_session, reports_org):
    # seed GBLT: date=2026-05-31, settled_date=2026-06-15, SETTLED expense
    ...
    ast = ReportsQuery(dataset=Dataset.TRANSACTIONS,
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
        dimensions=[Dimension.MONTH])
    rows, _ = await execute_query(db_session, ast, org_id=reports_org.id)
    by_month = {r["month"]: r["value"] for r in rows}
    assert "2026-06" in by_month and "2026-05" not in by_month
```
- [ ] **Step 2: Run → FAIL** (`-p team-txdate ... pytest tests/services/test_reports_query_service.py::test_month_bucketing_uses_settled_date -v`) — currently buckets to 2026-05.
- [ ] **Step 3: Implement** — in `_dimension_expr`, replace `Transaction.date` with `effective_period_date_expr()` inside the three time-bucket expressions. Import the helper: `from app.services.transaction_filters import effective_period_date_expr`. E.g.:
```python
    eff = effective_period_date_expr()
    if dim is Dimension.MONTH:
        if dialect_name == "sqlite":
            return func.strftime("%Y-%m", eff)
        return func.date_format(eff, "%Y-%m")
    if dim is Dimension.WEEK:
        if dialect_name == "sqlite":
            return func.strftime("%Y-%W", eff)
        return func.date_format(eff, "%x-%v")
    if dim is Dimension.DAY:
        if dialect_name == "sqlite":
            return func.strftime("%Y-%m-%d", eff)
        return func.date_format(eff, "%Y-%m-%d")
```
- [ ] **Step 4: Run → PASS.** Also run the full `test_reports_query_service.py` — existing time-grouping tests that used same-day date/settled rows stay green (settled_date defaults to date for those).
- [ ] **Step 5: Commit** — `git add backend/app/services/reports_query_service.py backend/tests/services/test_reports_query_service.py && git commit -m "fix(reports): bucket time dimensions by effective settled date"`

### Task 2: Reports DATE filter column
**Files:** Modify `reports_query_service.py` (`_FILTER_COLUMN[FilterField.DATE]`, ~`:144`). Test: same file.

- [ ] **Step 1: Failing test** — a report with a `date BETWEEN 2026-06-01..2026-06-30` filter INCLUDES the GBLT (settled June, dated May).
```python
@pytest.mark.asyncio
async def test_date_filter_uses_settled_date(db_session, reports_org):
    # GBLT date=2026-05-31 settled=2026-06-15
    ast = ReportsQuery(dataset=Dataset.TRANSACTIONS,
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
        filters=[Filter(field=FilterField.DATE, op=FilterOp.BETWEEN,
                        value=["2026-06-01","2026-06-30"])])
    rows, meta = await execute_query(db_session, ast, org_id=reports_org.id)
    assert meta["row_count"] == 1  # GBLT included by its June settled date
```
- [ ] **Step 2: Run → FAIL** (GBLT excluded today; its raw date is May).
- [ ] **Step 3: Implement** — `_FILTER_COLUMN[FilterField.DATE] = effective_period_date_expr()` (the module already will import the helper from Task 1; if Task 2 lands first, add the import). The BETWEEN/scalar coercion is unchanged (comparing a coalesce(date,date) expression to coerced dates works).
- [ ] **Step 4: Run → PASS** + full file green.
- [ ] **Step 5: Commit** — `git commit -am "fix(reports): filter by effective settled date"`

### Task 3: forecast_service period windows
**Files:** Modify `backend/app/services/forecast_service.py` (`Transaction.date` window predicates at ~`:96-97,:107-108,:165-166`). Test: `backend/tests/services/test_forecast_service.py` (or nearest).

- [ ] **Step 1: Failing test** — a settled-in-June GBLT counts toward the June forecast window, not May. (Mirror the file's existing forecast-period test; assert the GBLT amount is in the June aggregate.)
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — `from app.services.transaction_filters import effective_period_date_expr`; replace each `Transaction.date >= p_start` / `<= p_end` window predicate in the reportable aggregation queries with `effective_period_date_expr() >= p_start` / `<= p_end`. (Leave any `Transaction.date` use that is NOT a period window — verify each of the ~6 sites is a period-window predicate before swapping; if any is a display/order-only use, leave it and note why.)
- [ ] **Step 4: Run → PASS** + full forecast-service tests green.
- [ ] **Step 5: Commit** — `git commit -am "fix(forecast): bucket forecast windows by effective settled date"`

### Task 4: forecast_plan_service period windows
**Files:** Modify `backend/app/services/forecast_plan_service.py` (`:531,:537-538,:572-573`). Test: `backend/tests/services/test_forecast_plan_service.py` (or nearest).

- [ ] **Step 1: Failing test** — GBLT settled-in-June counts in the June plan period (and the 3-months-history window uses effective date). Mirror the file's existing period test.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — import the helper; swap the period-window `Transaction.date` predicates (`>= p_start`, `<= p_end`, the `>= three_months_ago` / `< p_start` history window) to `effective_period_date_expr()`. The `select(Transaction.date, ...)` at `:531` — if it's selecting the date for display/grouping in the plan output, decide: if it groups by period, swap to effective; if it's surfaced as the literal transaction date, leave it and note. Classify before swapping.
- [ ] **Step 4: Run → PASS** + full file green.
- [ ] **Step 5: Commit** — `git commit -am "fix(forecast-plan): bucket plan windows by effective settled date"`

### Task 5: scenario_engine history window
**Files:** Modify `backend/app/services/scenario_engine.py` (`:668-669`). Test: `backend/tests/services/test_scenario_engine.py`.

- [ ] **Step 1: Failing test** — a settled-in-June GBLT falls in the correct history bucket by settled date.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — import the helper; swap the `Transaction.date >= history_start` / `< today.replace(day=1)` window predicates to `effective_period_date_expr()`.
- [ ] **Step 4: Run → PASS** + full scenario tests green.
- [ ] **Step 5: Commit** — `git commit -am "fix(scenario): bucket history window by effective settled date"`

### Task 6: Budget alignment to the shared expression
**Files:** Modify `backend/app/services/budget_service.py` (`:47,:52`) + `backend/app/services/budget_rebalance_service.py` (`:203-204,:216,:219`). Test: `backend/tests/services/test_budget_service.py` (+ rebalance test).

- [ ] **Step 1: Failing test** — a PENDING transaction with a `settled_date` ESTIMATE inside the period now counts toward the budget (today it counts via raw settled_date which is set, so this specifically tests the fallback consistency: a row with settled_date NULL but `date` in-period should be handled identically to the rest of the app). Concretely: assert a settled-in-period GBLT counts (unchanged) AND that the query now uses `effective_period_date_expr()` (behavior-identical for settled rows; the change is the coalesce fallback for the NULL-settled_date case).
```python
# settled GBLT still counts in its settled month (regression guard)
# + a pending row with settled_date=None, date in-period: now included via coalesce fallback
```
- [ ] **Step 2: Run → FAIL** on the fallback case (NULL settled_date row excluded today).
- [ ] **Step 3: Implement** — replace `Transaction.settled_date >= period_start` / `<= period_end` (and the rebalance 3-month + current windows) with `effective_period_date_expr() >= period_start` / `<= period_end`. Update the `# Use settled_date ...` comment to reference the shared expression. Behavior-identical for settled rows; adds the documented date-fallback for NULL-settled_date rows.
- [ ] **Step 4: Run → PASS** + full budget tests green.
- [ ] **Step 5: Commit** — `git commit -am "refactor(budgets): align period bucketing to effective_period_date_expr"`

### Task 7: Phase-1 regression sweep + scope-guard verification
- [ ] **Step 1:** `docker compose -p team-txdate exec -T backend pytest tests/ -k "report or forecast or budget or scenario or transaction" -q` — all green.
- [ ] **Step 2: Scope guard** — confirm `recurring_service.py`, `import_service.py`, `transaction_suggestions_service.py` were NOT changed (`git diff --stat` shows none) and their tests pass. These keep raw `date` on purpose (due-date matching, dedup window, last-used).
- [ ] **Step 3:** Commit any fixup (`git commit -am "test(txdate): phase-1 backend sweep green" || true`).

---

## Phase 2 — Frontend: dual-date display (Date + Settled) everywhere a transaction renders

> The `settled_date` field is already on `TransactionResponse`/the `Transaction` TS type. Confirm in `frontend/lib/types.ts` (or `lib/reports/types.ts`); if absent on the relevant type, add it (`settled_date: string | null`). Labels: **"Date"** = `date`, **"Settled"** = `settled_date` (render `settled_date` or "—" when null). Pending rows: show the estimate or "—".

### Task 8: Transactions list — Settled column
**Files:** Modify `frontend/app/transactions/page.tsx` (the inline table: header row with the "Date" `<th>` and the body cell rendering `tx.date`). Test: `frontend/tests/app/transactions-settled-column.test.tsx` (new; mirror existing transactions-page test harness).

- [ ] **Step 1: Failing test** — render the list with a GBLT row (`date:"2026-05-31"`, `settled_date:"2026-06-15"`); assert both a "Date" cell (2026-05-31) and a "Settled" cell (2026-06-15) are present; a row with `settled_date:null` shows "—" in Settled.
- [ ] **Step 2: Run → FAIL** (`docker compose exec frontend npx vitest run tests/app/transactions-settled-column.test.tsx`).
- [ ] **Step 3: Implement** — add a "Settled" `<th>` next to the existing "Date" header, and a body `<td>` rendering `formatDate(tx.settled_date)` or "—". Keep the existing "Date" column (now explicitly the original date). Match the existing date formatting/util used for `tx.date`.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git add frontend/app/transactions/page.tsx frontend/tests/app/transactions-settled-column.test.tsx && git commit -m "feat(transactions): show Settled date column in the list"`

### Task 9: Dashboard recent transactions — show Settled
**Files:** Modify `frontend/app/dashboard/page.tsx` (the recent-transactions render). Test: `frontend/tests/app/dashboard-recent-settled.test.tsx` (new; mirror dashboard test harness).

- [ ] **Step 1: Failing test** — the dashboard recent-transactions list renders the settled date for a GBLT row (visible, not hidden); null → "—".
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — surface `settled_date` alongside the shown date. If the recent list is space-constrained, show Settled as the primary date with the original date as a secondary line/tooltip — but it must be visibly present (per the operator: "we can't hide such information"). Reuse the list's date format util.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(dashboard): show settled date on recent transactions"`

### Task 10: Account-detail recent transactions + any other list
**Files:** Modify `frontend/app/accounts/page.tsx` (and any account-detail recent-transactions view it renders). Test: new, mirroring the page's harness.

- [ ] **Step 1: Failing test** — if `app/accounts/page.tsx` renders transaction rows with a date, assert the settled date is shown. (If it does NOT render transactions, SKIP this task and note it — `git diff` shows no change.)
- [ ] **Step 2: Run → FAIL** (if applicable).
- [ ] **Step 3: Implement** — same Date+Settled pattern as Task 8.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(accounts): show settled date on account transactions" || true`

### Task 11: Transaction detail/edit form + transfer/batch modals
**Files:** Modify `frontend/components/floating/TransactionForm.tsx` and audit `frontend/components/transactions/{LinkAsTransferModal,MarkAsTransferModal,ImportMarkAsTransferModal,UnpairTransferModal,BatchEditModal}.tsx` + `frontend/app/transactions/batch/page.tsx` for any place a transaction's date is DISPLAYED (read-only). Test: new, per the form harness.

- [ ] **Step 1: Failing test** — where `TransactionForm` (or a modal) shows an existing transaction's date read-only, assert the settled date is also shown. (The editable date input is the original `date`; if the form has a settled-date input/field already, ensure it's visible. For modals that show a transaction summary line with a date, add the settled date.)
- [ ] **Step 2: Run → FAIL** (for surfaces that display a date today).
- [ ] **Step 3: Implement** — add the Settled date display next to each shown date. For surfaces that only show the original date in a summary, append the settled date. Do not add settled-date editing unless the form already edits it.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(transactions): surface settled date in form + transfer/batch modals"`

### Task 12: CSV export (if a transactions CSV exists)
**Files:** Locate the transactions CSV export (grep `frontend/` for the transactions export/download; if it lives backend-side, find the export endpoint/serializer). Test: per the export's existing test.

- [ ] **Step 1:** Locate the transactions CSV export. If NONE exists (only the reports-widget CSV does), SKIP and note it in the final report.
- [ ] **Step 2: Failing test** — the CSV includes a `settled_date` column with the row's settled date.
- [ ] **Step 3: Implement** — add `settled_date` to the CSV header + row mapping, next to the existing date column.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(transactions): add settled_date to CSV export" || true`

### Task 13: Frontend verification gate
- [ ] **Step 1:** `docker compose exec frontend npx eslint . --quiet` (CI gate — must be clean).
- [ ] **Step 2:** `docker compose exec frontend npx tsc --noEmit`.
- [ ] **Step 3:** `docker compose exec frontend npx vitest run` (FULL suite; known flake `settings-ai-providers-page > "PUTs the default routing payload on save"` passes in isolation).
- [ ] **Step 4:** Commit any fixup.

---

## Final: cross-surface consistency + review
- [ ] A cross-surface check (manual or test): the GBLT-style fixture appears in the **June** period in the list, the report (month grouping + date filter), and the forecast — same period everywhere.
- [ ] Fleet review per the "ship clean" bar, fold all confirmed findings, then open the PR. Title: `feat(transactions): cash-basis effective-date bucketing + Date/Settled display`. No test-plan section, no AI attribution.

---

## Self-review (plan author)
- **Spec coverage:** bucketing laggards → Tasks 1-5; budget alignment → Task 6; scope guard → Task 7; dual-date display on every surface → Tasks 8-12 (list, dashboard, accounts, form/modals, CSV); cash-basis app-wide → encoded by Tasks 1-6 + the consistency check. ✓
- **Placeholder scan:** the "if NONE exists, SKIP and note" on Tasks 10/12 are explicit conditional-skip instructions (the surface may not render transactions / a transactions CSV may not exist), each with a `|| true` commit guard — not vague TODOs. The forecast-service tasks instruct classify-before-swap because the exact predicates vary per file; the transformation (period-window `Transaction.date` → `effective_period_date_expr()`) is concrete. ✓
- **Consistency:** `effective_period_date_expr()` (import from `app.services.transaction_filters`) used identically across all backend tasks; "Date"/"Settled" labels + `settled_date`/"—" rendering used identically across frontend tasks. ✓
