# W4 Tier B — Phase 2a (DashboardDataProvider + 3 component tiles as widgets) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Establish the dashboard-widget pattern end-to-end at low risk: a **scoped** `DashboardDataProvider` (only the data the 3 already-component tiles need), a dashboard widget registry, the period-navigator as canvas chrome, and the gated `CustomDashboard` rendering the OnTrack / Accounts / Account-forecast tiles from a default layout. The 4 inline tiles (charts + recent-tx) come in Phase 2b/2c, growing the provider as needed.

**Architecture:** A NEW provider (flag-on path only) duplicates the relevant fetch/state from `LegacyDashboard` — the **legacy dashboard (flag OFF, production) is NOT touched**; the duplication is temporary and removed when the flag flips on (legacy deleted). Dashboard widget kinds (`dash_*`) live in a dashboard-scoped module and render via a `renderDashboardWidget` that composes with the existing Reports `renderWidgetByType` (so a dashboard can hold both finance tiles and report-cloned analytic widgets).

**Tech Stack:** Next.js 16 / React 19 / TS / Tailwind / SWR-ish fetch, Vitest.

## Global Constraints

- **Prod safety:** `Feature.CUSTOM_DASHBOARD` is OFF by default; `LegacyDashboard` (the flag-off path) must stay byte-identical — do NOT modify it. All Phase 2a code is reached only when the flag is ON.
- **No Off-Token**; **`npm run lint`** in verify (eslint `no-explicit-any` CI-gated) → [[reference_eslint_ci_gate_misses]]. No `as any`.
- **No AI attribution** in commits or PR body → [[feedback_no_ai_attribution]].
- **Reuse, don't fork:** Reports `Canvas`/`WidgetShell`/`renderWidgetByType`/`lib/reports/layout.ts`/`lib/reports/stack.ts`, and `lib/dashboard/api.ts` (Phase 1). The 3 tile components (`OnTrackTile`, `AccountTile`/`AccountTilesCard`, `AccountMonthEndForecast`) are reused AS-IS — widgets are thin wrappers that read the provider and pass the tiles their existing props.
- Tests in the frontend container: `docker compose exec frontend <cmd>`. Branch `feat/w4-tierB-phase2a-dashboard-widgets` (off main; has Phase 1).
- **Source of truth to replicate:** `frontend/app/dashboard/page.tsx` `LegacyDashboard` — its `loadRefs`/`loadForecastProjection`/`loadAccountMonthEndForecast`/`loadPendingTransactions`, the period-derivation memos (`selectedPeriod`, `realPeriodStart`, `isCurrent/Past/FutureSelectedPeriod`, `monthFrom/monthTo`, `pendingByAccount`), and `refreshAllPostWrite` + the `pfv:transaction-added` listener. READ it; reproduce behavior faithfully for the SCOPED subset below.

---

### Task 1: Scoped `DashboardDataProvider`

**Files:**
- Create: `frontend/components/dashboard/DashboardDataProvider.tsx`
- Test: `frontend/tests/components/dashboard/dashboard-data-provider.test.tsx`

**Interfaces:**
- Produces: `<DashboardDataProvider>` + `useDashboard()` exposing ONLY the Phase-2a subset:
```ts
interface DashboardData {
  accounts: Account[];
  activeAccounts: Account[];          // accounts filtered to active
  pendingByAccount: Record<number, number>;
  forecast: ForecastPlanLike | null;
  forecastProjection: ForecastProjectionLike | null;
  projectionFailed: boolean;
  projectionLoading: boolean;
  onRetryProjection: () => void;
  accountMonthEndForecast: AccountMonthEndForecastResponse | null;
  accountMonthEndForecastError: boolean;
  // period
  periods: BillingPeriod[];
  periodIdx: number;
  setPeriodIdx: (i: number) => void;
  selectedPeriod: BillingPeriod | null;
  isCurrentSelectedPeriod: boolean;
  isPastSelectedPeriod: boolean;
  isFutureSelectedPeriod: boolean;
  monthFrom: string;
  monthTo: string;
  jumpToCurrentPeriod: () => void;
  // status
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
}
export function useDashboard(): DashboardData  // throws if outside provider
```
(Transactions/budgets/chartFilter/chart-memoizations/status-mutation are deliberately OUT — added in 2b/2c.)

- [ ] **Step 1: READ `LegacyDashboard`** in `app/dashboard/page.tsx` — copy the EXACT fetch logic + period memos for the subset above (endpoints, the page-0 nuances are irrelevant here since 2a omits the tx table — only `loadRefs`, `loadForecastProjection`, `loadAccountMonthEndForecast`, `loadPendingTransactions`, and the period derivations + `pendingByAccount`). Note the `apiFetch` wrapper used.
- [ ] **Step 2: Write the failing test.** With the fetch layer mocked: assert the provider fetches refs/forecast/projection/account-forecast on mount, exposes the period derivations (current vs past vs future), `pendingByAccount` is computed, `onRetryProjection` re-fetches the projection, `jumpToCurrentPeriod` sets `periodIdx` to the current period, and a `pfv:transaction-added` event triggers `refresh()`. Mock `apiFetch` like other dashboard/reports tests do (READ an existing one).
- [ ] **Step 3: Run, verify fail.**
- [ ] **Step 4: Implement** the provider reproducing the LegacyDashboard subset (same endpoints, same period math, same non-blocking projection-failure semantics, same `pfv:transaction-added` listener calling `refresh()` which re-runs the 4 fetches via `Promise.allSettled`). `setPeriodIdx` clamps to `[0, periods.length-1]`.
- [ ] **Step 5: Run, verify pass.** Then `docker compose exec frontend npx tsc --noEmit`.
- [ ] **Step 6: Commit.** `feat(dashboard): scoped DashboardDataProvider (refs/forecast/period for component tiles)`

---

### Task 2: Dashboard widget registry (3 component tiles)

**Files:**
- Create: `frontend/lib/dashboard/widget-types.ts` (dashboard-scoped widget kinds + configs)
- Create: `frontend/components/dashboard/widgets/{OnTrackWidget,AccountsWidget,AccountForecastWidget}.tsx`
- Create: `frontend/components/dashboard/renderDashboardWidget.tsx` (dispatch + `emptyDashboardWidget` factory)
- Test: `frontend/tests/components/dashboard/dashboard-widget-registry.test.tsx`

**Interfaces:**
- Produces:
```ts
// widget-types.ts
type DashboardWidgetType = "dash_on_track" | "dash_accounts" | "dash_account_forecast"; // grows in 2b/2c
// dashboard widgets reuse the reports Widget shape {id,type,title,grid,config} with config = {} (data comes from the provider, not per-widget config)
export function emptyDashboardWidget(type: DashboardWidgetType, id: string): Widget;
// renderDashboardWidget.tsx
export function renderDashboardWidget(w: Widget): ReactNode; // dash_* → tile widget; else → reports renderWidgetByType(w, ...)
```
- Consumes: `useDashboard()` (Task 1); the 3 tile components.

- [ ] **Step 1: Build the 3 thin widget wrappers** — each reads `useDashboard()` and renders the existing tile with its existing props:
  - `OnTrackWidget` → `<OnTrackTile forecastPlan={forecast} projection={forecastProjection} projectionFailed={...} projectionLoading={...} onRetryProjection={...} isPastPeriod={isPastSelectedPeriod} isFuturePeriod={isFutureSelectedPeriod} />`
  - `AccountsWidget` → `<AccountTilesCard accounts={activeAccounts} pendingByAccount={pendingByAccount} />`
  - `AccountForecastWidget` → `<AccountMonthEndForecast forecast={accountMonthEndForecast} isCurrentPeriod={isCurrentSelectedPeriod} onJumpToCurrent={jumpToCurrentPeriod} hasAnyAccounts={activeAccounts.length>0} hasError={accountMonthEndForecastError} />`
- [ ] **Step 2: `widget-types.ts`** — the `DashboardWidgetType` union + `emptyDashboardWidget(type,id)` returning a `Widget` with sane default `grid` per tile (on_track full-width hero `{x:0,y:0,w:12,h:3}`; accounts `{x:0,y:3,w:4,h:5}`; account_forecast `{x:4,y:3,w:8,h:5}`) and `config: {}`.
- [ ] **Step 3: `renderDashboardWidget(w)`** — switch on `w.type`: the 3 `dash_*` → the wrappers; default → delegate to reports `renderWidgetByType(w, canvasFilters, editMode, currency)` (so report-cloned widgets still render). Read how CustomDashboard currently calls renderWidgetByType to keep the signature compatible.
- [ ] **Step 4: Test** — `emptyDashboardWidget` returns valid widgets; `renderDashboardWidget` renders each tile (mock `useDashboard`); a non-dash type falls through to the reports renderer. Real assertions.
- [ ] **Step 5: tsc + the new tests pass. Commit.** `feat(dashboard): dashboard widget registry + 3 finance tile widgets`

---

### Task 3: Wire CustomDashboard + period-nav chrome + default layout

**Files:**
- Modify: `frontend/components/dashboard/CustomDashboard.tsx`
- Create: `frontend/components/dashboard/DashboardPeriodNav.tsx` (chrome)
- Modify: `backend/app/routers/dashboard.py` `DEFAULT_DASHBOARD_LAYOUT` → the 3 dash_* tiles (Phase 2a default)
- Test: extend `frontend/tests/app/custom-dashboard.test.tsx`

- [ ] **Step 1:** Wrap `CustomDashboard`'s content in `<DashboardDataProvider>`. Render `<DashboardPeriodNav>` (fixed chrome above the Canvas — reproduce the LegacyDashboard period-nav JSX: ◀ / month label / ▶ / CURRENT badge or Today button + "View All Transactions" link, driving `periodIdx`/`jumpToCurrentPeriod` from `useDashboard`). The Canvas renders widgets via `renderDashboardWidget` instead of the reports `renderWidgetByType` (so dash_* tiles work; report widgets still fall through).
- [ ] **Step 2:** Update the server `DEFAULT_DASHBOARD_LAYOUT` (`backend/app/routers/dashboard.py`) to the 3 dash_* tiles (on_track, accounts, account_forecast) at the grid coords above — so a fresh GET returns today's top-of-dashboard arrangement. Update/extend the backend dashboard tests that assert the default layout shape.
- [ ] **Step 3:** Keep Customize/Save working (the layout now contains dash_* widgets; saving/loading them round-trips as plain `{id,type,title,grid,config}` — verify the layout validator accepts the `dash_*` types; if the backend `LayoutJson`/Widget validator is a closed union that rejects `dash_*`, widen it to allow dashboard widget types OR relax the dashboard layout validation to accept any string `type` with valid grid — decide + note; the reports validator must stay strict for reports).
- [ ] **Step 4: Tests** — flag ON: the period nav + the 3 tiles render (mock `useDashboard` or the fetch layer); Save still calls `saveDashboard` with the dash_* layout. Flag OFF: legacy unchanged (existing test). 
- [ ] **Step 5: tsc + lint + FULL suite + backend dashboard tests green. Commit.** `feat(dashboard): CustomDashboard renders finance-tile widgets + period nav + default layout`

---

### Task 4: Verification
- [ ] `docker compose exec frontend npx tsc --noEmit` clean; `npm run lint` 0 errors; full FE suite green; `docker compose exec backend pytest tests/routers/test_dashboard.py -q` green (default-layout change).
- [ ] Manual (flag force-ON): `/dashboard` shows the period nav + OnTrack hero + Accounts + Account-forecast as draggable widgets; Customize → move → Save → reload persists; period arrows/Today re-fetch the tiles; flag OFF → today's dashboard unchanged. Mobile → read-only stack.

## Decisions / risks
- **Provider duplicates LegacyDashboard logic (temporary).** Legacy stays untouched for prod safety; the duplication is deleted when the flag flips on and Legacy is removed (Phase 3/launch). Documented so it isn't mistaken for accidental divergence.
- **Layout validator + `dash_*` types** (Task 3 Step 3) is the one real cross-cutting decision — the dashboard layout must accept dashboard widget types while the reports validator stays strict. Resolve by validating dashboard layouts against a widened/dashboard-specific schema, not by loosening the reports one.

## Self-review (done)
- **Spec coverage:** Phase 2 (widgetize tiles) START — the scoped provider + registry + 3 low-risk tiles + period nav + default layout; charts (2b) + recent-tx (2c) explicitly deferred, provider grows then.
- **Placeholders:** fetch bodies defer to the named LegacyDashboard source (read + replicate the subset); interfaces/grids are concrete.
- **Type consistency:** `useDashboard`/`DashboardData`, `DashboardWidgetType`/`emptyDashboardWidget`/`renderDashboardWidget` consistent across tasks; widgets reuse the reports `Widget` shape.
