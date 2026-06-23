# W3 PR2 — Cash-flow Sankey widget — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add a Monarch-style cash-flow Sankey as a new Reports widget — income sources → an Income hub → spending categories (+ Income→Savings when income > expense) — using `@nivo/sankey`, backed by a dedicated endpoint that reuses the transactions query machinery.

**Architecture:** A new `POST /api/v1/reports/query/sankey` endpoint builds two aggregations (income-by-category, expense-by-category) by constructing two existing `ReportsQuery` ASTs and calling `execute_query()` (reusing org/date/filter/cash-basis machinery), then assembles `{source,target,value}` links. The frontend adds a `"sankey"` widget kind rendered with Nivo's `ResponsiveSankey`, wired into the canvas/picker/editor like the existing kinds, reusing the shared date bar + per-widget filter chips.

**Tech Stack:** FastAPI / SQLAlchemy async / Pydantic v2 (backend), Next.js 16 / React 19 / TS / `@nivo/sankey` (frontend), Vitest + pytest.

## Global Constraints

- **No Off-Token Rule** — Sankey colors come from `CHART_SERIES` (`frontend/lib/chart-colors.ts`, the 8 `var(--color-chart-N)` tokens shipped in PR1). Nivo accepts CSS-var strings as SVG fills. No bare hex / raw Tailwind palette colors.
- **Org-scoped, auth-gated** — endpoint uses `Depends(get_current_user)`, takes `org_id` from `current_user.org_id` (NEVER from the wire), under the existing `require_feature(Feature.REPORTS)` router gate + `@limiter.limit("60/minute")`.
- **Cash-basis** — bucket/period semantics via the existing `execute_query` path (`effective_period_date_expr()`), consistent with [[reference_effective_period_date_cash_basis]].
- **Transfer legs must NOT double-count** — transfer pairs are two rows typed `income`/`expense` (by direction), linked by `linked_transaction_id`; the reportable filter (`linked_transaction_id IS NULL AND is_manual_adjustment = False AND reconciliation_state NOT IN ('skipped','rejected')`, in `backend/app/services/transaction_filters.py::reportable_transaction_filter`) excludes them. The Sankey MUST only count reportable income/expense.
- **Hard caps** — reuse the schema caps (MAX_FILTERS=20, MAX_DATE_WINDOW_DAYS) from `reports_query.py`.
- **Backend tests in an isolated compose project when run by a parallel agent** (`-p team-<name>`), per CLAUDE.md — but this is the main session's stack; run via `docker compose exec backend pytest ...`.
- **Frontend dep gotcha** — `node_modules` is baked into the dev image (not volume-mounted); `package.json` is NOT mounted. Adding a dependency REQUIRES `docker compose up --build -d frontend`. The `.dockerignore` excludes host `node_modules` (prevents darwin-binary contamination) — do not work around it.
- **No AI attribution** in commits.
- **Sankey structure (locked):** income categories → **Income** hub → spending categories; add an **Income → Savings** link when Σincome > Σexpense (omit if expense ≥ income). `spending_granularity` ∈ {`category`,`category_master`} controls the spending side. `top_n` folds small spending categories into **Other**. Transfers excluded. Empty state when there is no income.

---

### Task 1: Add Nivo dependencies + rebuild dev image

**Files:**
- Modify: `frontend/package.json` (+ `package-lock.json` via install)

**Interfaces:**
- Produces: `@nivo/sankey` + `@nivo/core` importable in the frontend container.

- [ ] **Step 1: Install the latest React-19-compatible versions on the host.** From `frontend/`, run `npm install @nivo/sankey @nivo/core` (this updates package.json + package-lock.json and resolves a version; do NOT hand-pick a version — let npm resolve the current stable, which supports React 19 per the W3 library research). If npm reports a peer-dep conflict with React 19, capture the message and report it (do not force with `--legacy-peer-deps` without flagging).
- [ ] **Step 2: Rebuild the frontend image** (node_modules is baked, not mounted): `docker compose up --build -d frontend`. Wait for it to come healthy.
- [ ] **Step 3: Verify the dep resolves in the container.** Run: `docker compose exec frontend npm list @nivo/sankey @nivo/core`. Expected: both listed with concrete versions, no "UNMET".
- [ ] **Step 4: Smoke the import.** Run: `docker compose exec frontend npx tsc --noEmit`. Expected: green (no module-resolution error). (No source imports it yet; this just confirms types are present.)
- [ ] **Step 5: Commit.**
```bash
git add frontend/package.json frontend/package-lock.json
git commit -m "build(frontend): add @nivo/sankey + @nivo/core for cash-flow Sankey"
```

---

### Task 2: Backend — SankeyQuery schema, builder, endpoint, tests

**Files:**
- Modify: `backend/app/schemas/reports_query.py` (add `SankeyQuery`, `SankeyLink`, `SankeyResponse`)
- Create: `backend/app/services/sankey_service.py` (the builder)
- Modify: `backend/app/routers/reports.py` (add `POST /query/sankey`)
- Test: `backend/tests/services/test_sankey_service.py` (create), `backend/tests/routers/test_reports_sankey.py` (create)

**Interfaces:**
- Produces (consumed by frontend Task 4):
  - Request `SankeyQuery`: `{ filters: list[Filter], spending_granularity: Literal["category","category_master"] = "category", top_n: int | None = None }` (dataset is implicitly transactions; measure is implicitly sum of amount). `extra="forbid"`.
  - Response `SankeyResponse`: `{ links: list[SankeyLink], meta: QueryMeta }`, `SankeyLink = { source: str, target: str, value: float }`.
- Consumes: `execute_query`, `ReportsQuery`, `Filter`, `FilterField`, `Dimension`, `Measure`, `Aggregation` from existing modules; `reportable_transaction_filter` semantics (already applied by `execute_query` if it filters reportables — STEP 1 verifies).

- [ ] **Step 1: VERIFY transfer/reportable handling in `execute_query`.** Read `backend/app/services/reports_query_service.py`. Determine whether `execute_query` already excludes transfer legs / manual adjustments / skipped-rejected (i.e. applies `reportable_transaction_filter` or equivalent) for the transactions dataset. Record the finding in the task report. 
  - **If it DOES:** the builder can construct `ReportsQuery` ASTs with a `txn_type` filter and trust the reportable exclusion. Proceed with Step 3 (AST-reuse path).
  - **If it does NOT:** the income/expense aggregation would include transfer legs (double-counting cash flow). Then implement the builder against the lower-level compile helpers and add an explicit `Transaction.linked_transaction_id.is_(None)` (+ manual-adjustment/reconciliation) predicate. Note the chosen path in the report.

- [ ] **Step 2: Write the failing builder test.** Create `backend/tests/services/test_sankey_service.py`. Seed an org with: 2 income txns (Salary 5000, Freelance 1000) in 2 income categories; 3 expense txns (Housing 2000, Food 800, Transport 400) in 3 categories; and ONE transfer pair (two linked rows, 500 each). Assert `build_sankey(...)` returns links:
  - `Salary → Income` (5000), `Freelance → Income` (1000)
  - `Income → Housing` (2000), `Income → Food` (800), `Income → Transport` (400)
  - `Income → Savings` (6000 − 3200 = 2800)
  - and that the transfer pair contributes NOTHING (no link references it; totals unaffected).
```python
# pseudocode of the key assertions — adapt to the seeding helpers in backend/tests/conftest.py
links = await build_sankey(db, org_id=org.id, query=SankeyQuery(filters=[]))
by = {(l.source, l.target): l.value for l in links}
assert by[("Salary", "Income")] == 5000
assert by[("Income", "Housing")] == 2000
assert by[("Income", "Savings")] == 2800
assert not any("→" for ...)  # no transfer-derived link; sum of Income-out == income total
```
- [ ] **Step 3: Run it, verify it fails.** Run: `docker compose exec backend pytest tests/services/test_sankey_service.py -v`. Expected: FAIL (no `build_sankey`).
- [ ] **Step 4: Implement the schemas** in `backend/app/schemas/reports_query.py` (`SankeyQuery`, `SankeyLink`, `SankeyResponse` as in Interfaces; `extra="forbid"`; reuse existing `Filter`, `QueryMeta`).
- [ ] **Step 5: Implement `build_sankey`** in `backend/app/services/sankey_service.py`:
  - Build an **income AST**: `ReportsQuery(dataset=transactions, measure=Measure(sum, amount), dimensions=[category], filters=query.filters + [Filter(txn_type eq income)])`; `rows_income = await execute_query(...)`.
  - Build an **expense AST**: same but `dimensions=[spending_granularity]` and `txn_type eq expense`; `rows_expense = await execute_query(...)`.
  - (If Step 1 found execute_query does NOT exclude transfers, instead use the lower-level path with an explicit reportable predicate — see Step 1.)
  - Links: for each income row `{cat, value}` → `SankeyLink(cat, "Income", value)`; for each expense row → `SankeyLink("Income", cat, value)`. Apply `top_n` to the expense side (fold the tail into `SankeyLink("Income","Other", Σtail)`).
  - `income_total = Σ income values`, `expense_total = Σ expense values`; if `income_total > expense_total`, append `SankeyLink("Income","Savings", income_total − expense_total)`.
  - Guard: if `income_total == 0`, return `[]` (frontend renders empty state). Avoid self-referential/cyclic links (a category literally named "Income"/"Savings"/"Other" is an acceptable edge case; do not special-case beyond ensuring no `source==target`).
  - Build `QueryMeta` from the combined row counts.
- [ ] **Step 6: Run the builder test, verify it passes.** Expected: PASS.
- [ ] **Step 7: Write + run the endpoint test.** Create `backend/tests/routers/test_reports_sankey.py`: authenticated `POST /api/v1/reports/query/sankey` returns 200 with the links for the seeded org; a second org's data is NOT included (org-scoping); unknown body key → 422 (`extra="forbid"`); feature-gate respected (mirror an existing reports router test's gating setup). Run: `docker compose exec backend pytest tests/routers/test_reports_sankey.py -v`. Expected: PASS.
- [ ] **Step 8: Add the endpoint** in `backend/app/routers/reports.py` mirroring `POST /query` (auth dep, `@limiter.limit("60/minute")`, `org_id=current_user.org_id`, response_model=`SankeyResponse`).
- [ ] **Step 9: Full backend check.** Run: `docker compose exec backend pytest tests/services/test_sankey_service.py tests/routers/test_reports_sankey.py -v`. Expected: all PASS.
- [ ] **Step 10: Commit.**
```bash
git add backend/app/schemas/reports_query.py backend/app/services/sankey_service.py backend/app/routers/reports.py backend/tests/services/test_sankey_service.py backend/tests/routers/test_reports_sankey.py
git commit -m "feat(reports): cash-flow Sankey endpoint (income hub, transfer-safe)"
```

---

### Task 3: Frontend types — sankey widget kind

**Files:**
- Modify: `frontend/lib/reports/types.ts`

**Interfaces:**
- Produces (consumed by Tasks 4-6): `WidgetType` gains `"sankey"`; `SankeyConfig`, `SankeyWidget`, `Widget` union includes `SankeyWidget`; wire types `SankeyLink`, `SankeyResponse`.

- [ ] **Step 1: Add the types.**
```ts
// in WidgetType union:  | "sankey"
export interface SankeyConfig {
  dataset: "transactions";
  measure: Measure;                 // sum of amount
  filters?: WidgetFilters;
  spending_granularity?: "category" | "category_master"; // default "category"
  top_n?: number;
}
export type SankeyWidget = BaseWidget<"sankey", SankeyConfig>;
// add SankeyWidget to the Widget union
export interface SankeyLink { source: string; target: string; value: number; }
export interface SankeyResponse { links: SankeyLink[]; meta: QueryMeta; }
```
- [ ] **Step 2: Typecheck.** Run: `docker compose exec frontend npx tsc --noEmit`. Expected: it will FAIL where `widgetKit`/picker switch on `WidgetType` exhaustively (that's expected — Tasks 5/6 add the cases). If tsc fails ONLY on missing `"sankey"` handling in those files, that's fine; if it fails elsewhere, fix the type. Note the expected failures in the report.
- [ ] **Step 3: Commit.**
```bash
git add frontend/lib/reports/types.ts
git commit -m "feat(reports): sankey widget types"
```

---

### Task 4: Frontend API + data hook

**Files:**
- Modify: `frontend/lib/reports/api.ts` (add `runSankeyQuery`)
- Create: `frontend/lib/reports/useSankeyQuery.ts`
- Test: `frontend/tests/lib/reports/use-sankey-query.test.ts` (create)

**Interfaces:**
- Consumes: Task 3 types; the existing filter-resolution used by `useReportQuery` (read `useReportQuery.ts` + `resolve.ts` to reuse `resolveFilters`/canvas-cascade).
- Produces (consumed by Task 5): `runSankeyQuery(body): Promise<SankeyResponse>` and `useSankeyQuery(widget, canvasFilters): { data, error, isLoading }`.

- [ ] **Step 1: Write the failing hook test** asserting `runSankeyQuery` POSTs to `/api/v1/reports/query/sankey` with resolved filters and returns `links`. Mock the fetch layer the same way existing `api.ts`/hook tests do (read `frontend/tests/lib/reports/` for the pattern).
- [ ] **Step 2: Run it, verify it fails.**
- [ ] **Step 3: Implement `runSankeyQuery`** in `api.ts` (mirror `runQuery`: same auth fetch wrapper, POST the `SankeyQuery` body, parse `SankeyResponse`).
- [ ] **Step 4: Implement `useSankeyQuery`** — build the `SankeyQuery` body from `widget.config` + resolved filters (reuse the canvas-date cascade + per-widget filter resolution from `useReportQuery`/`resolve.ts`; do NOT duplicate the resolver — import it), SWR-key on the serialized body, call `runSankeyQuery`.
- [ ] **Step 5: Run test, verify it passes.** Then `docker compose exec frontend npx tsc --noEmit`.
- [ ] **Step 6: Commit.**
```bash
git add frontend/lib/reports/api.ts frontend/lib/reports/useSankeyQuery.ts frontend/tests/lib/reports/use-sankey-query.test.ts
git commit -m "feat(reports): sankey query api + data hook"
```

---

### Task 5: Frontend components — SankeyWidget + chart

**Files:**
- Create: `frontend/components/reports/widgets/SankeyWidget.tsx`, `frontend/components/reports/widgets/SankeyWidgetChart.tsx`
- Test: `frontend/tests/components/reports/sankey-widget.test.tsx` (create)

**Interfaces:**
- Consumes: `useSankeyQuery` (Task 4), `CHART_SERIES` (`@/lib/chart-colors`), `WidgetShell` (existing wrapper used by other widgets — read one, e.g. `BarWidget.tsx`, to match the shell/loading/empty conventions).
- Produces (consumed by Task 6): default-exported `SankeyWidget` taking the same props the other widgets take (`widget`, `canvasFilters`, `editMode` — confirm exact prop names from `widgetKit.tsx`).

- [ ] **Step 1: Write the failing test.** With `useSankeyQuery` mocked: (a) given links, the component renders the Nivo chart container; (b) given empty links, it renders the empty state copy "No income in this period to chart cash flow". Mock `@nivo/sankey`'s `ResponsiveSankey` (jsdom can't lay it out) the way the area/bar tests mock recharts — assert it receives `data` with the expected nodes/links and a `colors` prop derived from `CHART_SERIES`.
- [ ] **Step 2: Run it, verify it fails.**
- [ ] **Step 3: Implement `SankeyWidgetChart.tsx`** — a client-only Nivo `ResponsiveSankey`. Convert `SankeyLink[]` → Nivo's `{ nodes:[{id}], links:[{source,target,value}] }` (derive unique node ids from the links). Pass `colors={CHART_SERIES}`. Load Nivo client-only via `next/dynamic(() => import(...), { ssr:false })` (consistent with the canvas; avoids SSR/window issues). Keep labels/tooltips on (Nivo defaults) for a11y.
- [ ] **Step 4: Implement `SankeyWidget.tsx`** — calls `useSankeyQuery`, renders `WidgetShell` with loading/empty/error states (match the other widgets), and `SankeyWidgetChart` when links exist.
- [ ] **Step 5: Run test, verify it passes.** Then `docker compose exec frontend npx tsc --noEmit`.
- [ ] **Step 6: Commit.**
```bash
git add frontend/components/reports/widgets/SankeyWidget.tsx frontend/components/reports/widgets/SankeyWidgetChart.tsx frontend/tests/components/reports/sankey-widget.test.tsx
git commit -m "feat(reports): SankeyWidget + Nivo chart with empty state"
```

---

### Task 6: Wire sankey into canvas/picker/editor

**Files:**
- Modify: `frontend/components/reports/widgetKit.tsx` (`emptyWidget`/`emptySankey` + `renderWidgetByType` case)
- Modify: `frontend/components/reports/WidgetPicker.tsx` (picker entry, new "Flow" group or under "Categories")
- Modify: editor config — check `frontend/components/reports/WidgetEditorPopover.tsx` + `frontend/components/reports/config/` for how a widget's Data/Style tabs are built; add a minimal Sankey editor surface exposing `spending_granularity` (category vs master) + `top_n`, reusing existing controls. If the editor is driven by the source catalog, ensure sankey's fixed config doesn't break the generic editor (it may only need the filters tab + the two knobs).
- Test: extend `frontend/tests/components/reports/widget-picker.test.tsx` (or the relevant kit test) to cover the new kind.

**Interfaces:**
- Consumes: `SankeyWidget` (Task 5), `SankeyConfig` defaults (Task 3).

- [ ] **Step 1: Write/extend the failing test** — `emptyWidget("sankey", id)` returns a valid `SankeyWidget` with sane defaults (`spending_granularity:"category"`, sum-amount measure, empty filters); the picker lists a "Cash flow" / Sankey option; `renderWidgetByType` returns `<SankeyWidget>` for a sankey widget. Read the existing kit/picker tests for the assertion style.
- [ ] **Step 2: Run it, verify it fails.**
- [ ] **Step 3: Implement** `emptySankey(id)` factory + `case "sankey": return <SankeyWidget .../>` in `renderWidgetByType`; add the picker entry (icon + label "Cash flow (Sankey)" + description) ; add the minimal editor knobs (spending_granularity radio + top_n) in the popover, reusing existing form primitives.
- [ ] **Step 4: Run test, verify it passes.** Then `docker compose exec frontend npx tsc --noEmit` (the exhaustive-switch errors from Task 3 should now be gone).
- [ ] **Step 5: Commit.**
```bash
git add frontend/components/reports/widgetKit.tsx frontend/components/reports/WidgetPicker.tsx frontend/components/reports/WidgetEditorPopover.tsx frontend/components/reports/config/ frontend/tests/
git commit -m "feat(reports): wire sankey widget into picker, canvas, editor"
```

---

### Task 7: Full verification + manual visual check

- [ ] **Step 1: Full suites.** Run: `docker compose exec frontend npx tsc --noEmit && docker compose exec frontend npm test` and `docker compose exec backend pytest tests/ -q` (or the sharded subset touching reports). Expected: all green.
- [ ] **Step 2: Design-token gate.** Run: `docker compose exec frontend bash scripts/check-design-tokens.sh`. Expected: exit 0 (no off-token colors introduced; Nivo uses `CHART_SERIES`).
- [ ] **Step 3: Manual visual verification.** With the stack running, open `/reports`, add a "Cash flow (Sankey)" widget, set a date range with real income+expense data, and confirm: nodes/links render with the Brass-Harmony palette, the Income hub sits in the middle, Savings appears when income > expense, labels are legible, and the empty state shows when no income. Screenshot dark + light. Toggle `spending_granularity` master↔category and confirm the spending side regroups.

## Self-review (done)

- **Spec coverage:** dep (T1), backend endpoint+transfer-safety+savings+empty (T2), types (T3), api/hook with filter reuse (T4), components + empty state + token colors (T5), canvas/picker/editor wiring + granularity/top_n knobs (T6), verification + visual (T7).
- **Placeholders:** test bodies are described with concrete seed data + assertions; exact seeding-helper names are deferred to the implementer reading `conftest.py` (acceptable — the assertions are concrete).
- **Type consistency:** `SankeyQuery`/`SankeyLink`/`SankeyResponse`/`SankeyConfig`/`build_sankey`/`runSankeyQuery`/`useSankeyQuery` names used consistently across tasks; `spending_granularity` spelled identically everywhere.
- **Risk flagged:** Task 2 Step 1 is a real fork (does `execute_query` exclude transfer legs?) — the plan handles both branches rather than assuming.
