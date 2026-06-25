# W4 Tier B — Phase 2b (chart tiles + cross-tile chartFilter) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Widgetize the 3 inline dashboard chart tiles (Spending-by-category donut, Budget-progress bars, Forecast-by-category bars), extending `DashboardDataProvider` with the data they need + the cross-tile `chartFilter` (set by the charts; its consumer — the recent-tx table — arrives in 2c). Also fold the deferred cleanup: extract a shared report-widget renderer. Still behind `Feature.CUSTOM_DASHBOARD` (OFF); legacy dashboard untouched.

**Architecture:** Grow the provider (added in 2a) with the period-scoped transactions snapshot + budgets + the chart memoizations + spending-sort + `chartFilter`. Extract the 3 inline chart sections from `LegacyDashboard` into widget components that read the provider. Register `dash_spending`/`dash_budget`/`dash_forecast_category` and add them to the dashboard layout validator + default layout. Replace the duplicated reports widget switch with one shared `renderReportWidget`.

**Tech Stack:** Next.js 16 / React 19 / TS / Tailwind / Recharts (W3 palette), FastAPI/Pydantic, Vitest + pytest.

## Global Constraints

- **Prod safety:** flag OFF by default; **do NOT modify `LegacyDashboard`** (`frontend/app/dashboard/page.tsx`) — it's the flag-off production path + the extraction *source* (copy from it; don't change it). The strict reports validator (`backend/app/schemas/report_layout.py`) stays unchanged.
- **No Off-Token**; **`npm run lint`** in verify (eslint `no-explicit-any` CI-gated) → [[reference_eslint_ci_gate_misses]]. No `as any`.
- **No AI attribution** in commits or PR body → [[feedback_no_ai_attribution]].
- Reuse the W3 chart widgets / `chart-colors`, the existing `usePersistedSort`, and the existing chart JSX from LegacyDashboard (copy, adapt to provider reads).
- Tests in the frontend container. Branch `feat/w4-tierB-phase2b-chart-tiles` (off main; has Phase 1+2a).
- **Extraction source (read, replicate, do NOT modify):** `frontend/app/dashboard/page.tsx` — the memos (`donutDataRaw`/`donutData`/`totalSpend`/`sortedSpending` ~554-594, `dashBudgets`/`budgetChartData` ~608-630, `forecastExpenseItems`/`forecastChartRows` ~632-650), `spendingSort`/`toggleSpendingSort`, `chartFilter`/`setChartFilter`, and the 3 chart JSX sections (Spending ~890-1064, Budget ~1066-1124, Forecast ~1126-1182).

---

### Task 1: Extract shared `renderReportWidget` (fold the 2a duplication)

**Files:**
- Create: `frontend/components/reports/renderReportWidget.tsx`
- Modify: `frontend/app/reports/[id]/page.tsx` (use the shared one), `frontend/components/dashboard/renderDashboardWidget.tsx` (fall-through → shared one)
- Test: `frontend/tests/components/reports/render-report-widget.test.tsx`

**Interfaces:**
- Produces: `export function renderReportWidget(w: Widget, canvasFilters: CanvasFilters, editMode: boolean, currency?: string): ReactNode` — the 9-case report widget switch (kpi/bar/line/area/stacked_bar/pie/sparkline/table/sankey). The sankey arm renders `SankeyWidget` for the reports page; the dashboard backend validator rejects sankey layouts, so a dashboard layout can never reach the sankey arm — it's unreachable from the dashboard path (not omitted).

- [ ] **Step 1: READ** the `renderWidgetByType` switch in `app/reports/[id]/page.tsx` and the duplicated fall-through in `renderDashboardWidget.tsx`. Move the switch verbatim into `renderReportWidget.tsx` (import the widget components there).
- [ ] **Step 2:** In `app/reports/[id]/page.tsx`, delete the local switch and call `renderReportWidget(...)`. In `renderDashboardWidget.tsx`, replace the fall-through copy with `renderReportWidget(...)`.
- [ ] **Step 3:** Test asserts `renderReportWidget` renders each report widget type (mock the widgets) and is the single source. Update any test that asserted on the page-local switch.
- [ ] **Step 4: tsc + lint + the reports-editor + dashboard registry tests pass. Commit.** `refactor(reports): shared renderReportWidget (de-dup dashboard + reports)`

---

### Task 2: Extend `DashboardDataProvider` with chart data + chartFilter

**Files:**
- Modify: `frontend/components/dashboard/DashboardDataProvider.tsx`
- Test: extend `frontend/tests/components/dashboard/dashboard-data-provider.test.tsx`

**Interfaces (added to `DashboardData`):**
```ts
allTransactions: Transaction[];        // full period snapshot (limit=200)
budgets: Budget[];
dashBudgets: Budget[];                 // first 6 (memo)
budgetChartData: BudgetChartRow[];     // memo (copy legacy shape)
donutData: DonutDatum[]; totalSpend: number; sortedSpending: SortedSpendingRow[];
spendingSort: PersistedSort<SpendingSort>; toggleSpendingSort: (f: SpendingSort) => void;
forecastExpenseItems: ForecastPlanItem[]; forecastChartRows: ForecastChartRow[];
chartFilter: string | null; setChartFilter: (c: string | null) => void;
```
(Reuse the exact row/shape types from LegacyDashboard — import or mirror.)

- [ ] **Step 1: READ** the LegacyDashboard memos + fetch for the snapshot/budgets. Add to the provider: a period-scoped **transactions snapshot** fetch (`GET /api/v1/transactions?limit=200&date_from=monthFrom&date_to=monthTo`) → `allTransactions`; re-add the **budgets** fetch (per-period: `GET /api/v1/budgets?period_start=realPeriodStart`) — both gated on `realPeriodStart`, with stale-request guards like the other loaders, refreshed by `refresh()` + period change.
- [ ] **Step 2:** Add the memos verbatim from legacy: `donutDataRaw`/`donutData`/`totalSpend`/`sortedSpending` (deps: allTransactions + spendingSort), `dashBudgets`/`budgetChartData` (deps: budgets), `forecastExpenseItems`/`forecastChartRows` (deps: forecast). Add `spendingSort = usePersistedSort(...)` + `toggleSpendingSort` (same persisted key as legacy). Add `chartFilter` state + `setChartFilter`.
- [ ] **Step 3:** `setPeriodIdx` (and `jumpToCurrentPeriod`) must **clear `chartFilter`** (match legacy period-nav behavior).
- [ ] **Step 4: Tests** — provider fetches the snapshot + budgets on period resolve; donut/budget/forecast memos compute correctly from seeded data; `toggleSpendingSort` flips sort; `setChartFilter` sets/clears; period change clears `chartFilter`. See fail/implement/pass.
- [ ] **Step 5: tsc + lint + tests. Commit.** `feat(dashboard): provider chart data (snapshot/budgets/memos) + chartFilter`

---

### Task 3: The 3 chart widget components + registry + validator

**Files:**
- Create: `frontend/components/dashboard/widgets/{SpendingDonutWidget,BudgetBarsWidget,ForecastBarsWidget}.tsx`
- Modify: `frontend/lib/dashboard/widget-types.ts` (3 new types + grids), `frontend/components/dashboard/renderDashboardWidget.tsx` (3 arms)
- Modify: `backend/app/schemas/dashboard.py` (add the 3 dash chart types to the dashboard widget union)
- Test: `frontend/tests/components/dashboard/chart-widgets.test.tsx` + extend backend dashboard tests

- [ ] **Step 1: Extract each chart section** from LegacyDashboard into a widget component that reads `useDashboard()`:
  - `SpendingDonutWidget` ← Spending section (~890-1064): donut + sortable legend + empty state; reads `donutData/totalSpend/sortedSpending/chartFilter/setChartFilter/spendingSort/toggleSpendingSort`. Reuse the W3 donut styling.
  - `BudgetBarsWidget` ← Budget section (~1066-1124): bars + legend + empty state; reads `dashBudgets/budgetChartData/chartFilter/setChartFilter/isPast/isFutureSelectedPeriod`.
  - `ForecastBarsWidget` ← Forecast section (~1126-1182): bars + empty state; reads `forecast/forecastChartRows/chartFilter/setChartFilter/isPast/isFutureSelectedPeriod`.
  Copy the JSX verbatim; swap page-state reads for provider reads. Token-only.
- [ ] **Step 2:** `widget-types.ts` — add `dash_spending`/`dash_budget`/`dash_forecast_category` to the union + `emptyDashboardWidget` grids (row 3, three across: e.g. each `{w:4,h:5}` at y:8, x:0/4/8). `renderDashboardWidget` — 3 new arms.
- [ ] **Step 3:** `backend/app/schemas/dashboard.py` — add the 3 dash chart types to the dashboard widget union (config `{}`); reports validator unchanged. Add backend tests: a layout with the 3 chart tiles round-trips; unknown still 422.
- [ ] **Step 4: Tests** — each chart widget renders from a mocked provider (data + empty states); clicking a slice/bar calls `setChartFilter`; `emptyDashboardWidget`/`renderDashboardWidget` cover the 3 new types. Real assertions.
- [ ] **Step 5: tsc + lint + FULL suite + backend dashboard tests. Commit.** `feat(dashboard): spending/budget/forecast chart widgets + validator types`

---

### Task 4: Default layout (6 tiles) + verification

**Files:**
- Modify: `backend/app/routers/dashboard.py` (`DEFAULT_DASHBOARD_LAYOUT` += 3 chart tiles), `backend/tests/routers/test_dashboard.py` (default shape)

- [ ] **Step 1:** Extend `DEFAULT_DASHBOARD_LAYOUT` to include the 3 chart tiles at the row-3 grid coords (matching `emptyDashboardWidget`), so a fresh GET returns the 2a tiles + the 3 charts (today's dashboard's top + chart rows). Update the default-shape test (now 6 tiles).
- [ ] **Step 2: Verify.** `docker compose exec frontend npx tsc --noEmit` clean; `npm run lint` 0 errors; full FE suite green; `docker compose exec backend pytest tests/routers/test_dashboard.py -q` green.
- [ ] **Step 3: Manual (flag force-ON):** `/dashboard` shows period nav + the 6 tiles (on_track, accounts, account-forecast, spending donut, budget bars, forecast bars) as draggable widgets; clicking a donut slice sets a filter chip (its tx-table effect lands in 2c); period change clears the filter; Customize/Save/reload persists; flag OFF → legacy unchanged; mobile read-only stack.

## Out of scope (2c/3)
- Recent-transactions tile (2c — the status-mutation tile + chartFilter's tx-table consumer).
- Add-from-report, reset-to-default, flag-flip (Phase 3).

## Self-review (done)
- **Spec coverage:** Phase 2 continues — the 3 chart tiles + provider growth (snapshot/budgets/memos/sort) + chartFilter (set now, consumed 2c) + the shared-renderer cleanup folded. recent-tx explicitly deferred to 2c.
- **Placeholders:** memo/JSX bodies defer to named LegacyDashboard line ranges (copy + swap to provider reads); types/grids/endpoints concrete.
- **Type consistency:** provider field names mirror LegacyDashboard; `dash_spending`/`dash_budget`/`dash_forecast_category` + `renderReportWidget` consistent across tasks.
