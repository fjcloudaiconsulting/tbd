# W4 Tier B — Phase 3 (add-from-report + reset + mobile) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the customizable dashboard — let users add widgets (re-add removed finance tiles AND clone analytic widgets from saved Reports, including Sankey), reset to the default layout, and render a legible mobile read-only stack — all still behind `Feature.CUSTOM_DASHBOARD` (OFF). A *separate* follow-up PR (Phase 3b, not in this plan's main PR) flips the flag and deletes LegacyDashboard.

**Architecture:** The dashboard already renders through the Reports `Canvas` from a per-user persisted `layout_json`. `renderDashboardWidget` already falls through to `renderReportWidget` for non-`dash_*` types, so cloned analytic widgets render + self-fetch via `useReportQuery` with no new render code. Phase 3 adds the missing *Customize-mode UX* (an Add-widget picker with a "Dashboard" re-add group and a "From a report" clone path), a Reset-to-default action sourced from a new non-persisting backend default endpoint (single source of truth = the existing server `DEFAULT_DASHBOARD_LAYOUT`), mobile stack heights for the seven `dash_*` types, and the one-line backend change to accept cloned Sankey widgets on the dashboard validator.

**Tech Stack:** Next.js 16 / React 19 / TypeScript / SWR (frontend), FastAPI / Pydantic v2 (backend), react-grid-layout 1.5 (the shared Canvas), Vitest (FE tests), pytest (BE tests).

## Global Constraints

- **Flag stays OFF.** All Phase 3 feature work ships behind `Feature.CUSTOM_DASHBOARD` (default OFF). Do NOT flip the flag or touch `LegacyDashboard` in this plan — that is Phase 3b, a separate PR after operator preview.
- **No Off-Token Rule** — token classes only; raw Tailwind palette colors are CI-blocked by `frontend/scripts/check-design-tokens.sh`. Arbitrary *size* utilities (e.g. `max-w-[1760px]`) are allowed; arbitrary *colors* are not.
- **The One Brass Rule / Sidebar-Always-Navy** unchanged. WCAG 2.2 AA — widgets keep labels/legends.
- **Verify set MUST include `npm run lint`** — eslint `no-explicit-any` is CI-gated and is NOT covered by local `tsc`/tests. Full FE verify = `npx tsc --noEmit` + `npm run lint` + `npm test`. Full BE verify = `pytest`.
- **No AI attribution** in commits OR PR bodies (no "Generated with Claude", no Co-Authored-By Claude). Hard project rule.
- **Backend tests in an agent session use an isolated compose project:** `docker compose -p team-phase3 exec backend pytest ...` — every compose/exec call carries `-p team-phase3`. Never run against the default `pfv` project (writes the user's MySQL volume).
- **Reports strict validator stays byte-for-byte unchanged.** `report_layout.py`'s `validate_layout_json` must keep rejecting all `dash_*` types (the no-smuggling guard). Only the *dashboard* validator gains Sankey.
- **Validate-as-side-effect, return verbatim.** Never `model_dump`-round-trip a layout blob (PR #424 lesson) — it silently strips unmodelled widget knobs.

---

## File Structure

**Backend**
- `backend/app/schemas/dashboard.py` — add `SankeyWidget` to the import + `_DashboardWidget` union (Task 1).
- `backend/app/routers/dashboard.py` — add `GET /api/v1/dashboard/default` returning the seed layout without persisting (Task 5).
- `backend/tests/schemas/test_dashboard_schemas.py`, `backend/tests/routers/test_dashboard.py` — sankey-accepted + default-endpoint tests.

**Frontend**
- `frontend/lib/dashboard/clone.ts` *(new)* — `cloneWidgetForDashboard(source, existing)`: deep-copy a report widget, fresh id, place at next free row (Task 2).
- `frontend/components/dashboard/AddWidgetMenu.tsx` *(new)* — the Customize-mode add-widget UI: a "Dashboard" group (re-add the 7 `dash_*` tiles) + a "From a report" path (pick report → pick widget → clone) (Tasks 3 & 4).
- `frontend/components/dashboard/CustomDashboard.tsx` — wire Add-widget button + `addWidget`/`addClonedWidget` handlers + Reset-to-default action into Customize mode (Tasks 3, 4, 5).
- `frontend/lib/reports/stack.ts` — extend `mobileStackHeight`/`orderWidgetsForStack` typing to `Widget | DashboardWidget` + add the 7 `dash_*` heights (Task 6).
- `frontend/lib/dashboard/api.ts` — add `getDefaultDashboard()` (Task 5).
- Tests colocated under `frontend/**/__tests__` / `*.test.tsx` per existing convention.

---

## Task 1: Backend — accept cloned Sankey widgets on the dashboard validator

**Files:**
- Modify: `backend/app/schemas/dashboard.py` (import block ~lines 40-50; `_DashboardWidget` union ~lines 131-151)
- Test: `backend/tests/schemas/test_dashboard_schemas.py`, `backend/tests/routers/test_dashboard.py`

**Interfaces:**
- Consumes: `SankeyWidget` (already exported from `app.schemas.report_layout`, added in PR #488).
- Produces: dashboard `layout_json` PATCH now accepts a `type:"sankey"` widget; reports strict validator unchanged.

- [ ] **Step 1: Write the failing schema test**

In `backend/tests/schemas/test_dashboard_schemas.py`, add a sankey layout helper + acceptance test (mirror the existing `_sankey_layout()` in `test_report_layout_validation.py:102-118`):

```python
def _sankey_widget():
    return {
        "id": "s1",
        "type": "sankey",
        "title": "Cash Flow",
        "grid": {"x": 0, "y": 0, "w": 8, "h": 5},
        "config": {
            "dataset": "transactions",
            "measure": {"agg": "sum", "field": "amount"},
            "spending_granularity": "category",
            "top_n": 12,
        },
    }


def test_dashboard_accepts_cloned_sankey_widget():
    layout = {"version": 1, "widgets": [_sankey_widget()]}
    # validates without raising; returns the blob verbatim
    out = validate_dashboard_layout_json(layout)
    assert out["widgets"][0]["config"]["top_n"] == 12
    assert out["widgets"][0]["config"]["spending_granularity"] == "category"
```

(Match the existing import of `validate_dashboard_layout_json` at the top of the test file.)

- [ ] **Step 2: Run it and confirm it fails**

Run: `docker compose -p team-phase3 exec backend pytest tests/schemas/test_dashboard_schemas.py::test_dashboard_accepts_cloned_sankey_widget -v`
Expected: FAIL — `Input tag 'sankey' ... does not match any of the expected tags`.

- [ ] **Step 3: Add SankeyWidget to the dashboard validator**

In `backend/app/schemas/dashboard.py`, add `SankeyWidget` to the `from app.schemas.report_layout import (...)` block (keep alphabetical with the others — after `PieWidget`, before `SparklineWidget`), and add `SankeyWidget,` to the `_DashboardWidget` `Union[...]` (append after `TableWidget,`).

- [ ] **Step 4: Run the schema test — confirm PASS**

Run: `docker compose -p team-phase3 exec backend pytest tests/schemas/test_dashboard_schemas.py::test_dashboard_accepts_cloned_sankey_widget -v`
Expected: PASS.

- [ ] **Step 5: Add the router-level acceptance test (PATCH round-trips sankey verbatim)**

In `backend/tests/routers/test_dashboard.py`, mirror `test_patch_accepts_chart_tile_types` (lines ~481-519) with a sankey widget; PATCH, assert 200, then GET and assert `config.top_n`/`config.spending_granularity` survive verbatim.

- [ ] **Step 6: Run the dashboard + report-layout suites — confirm the no-smuggling guard still holds**

Run: `docker compose -p team-phase3 exec backend pytest tests/schemas/test_dashboard_schemas.py tests/routers/test_dashboard.py tests/schemas/test_report_layout_validation.py -q`
Expected: all pass — including `test_reject_dashboard_native_types_no_smuggling` (reports validator still rejects `dash_*`). The reports validator file is NOT modified.

- [ ] **Step 7: Commit**

```bash
git add backend/app/schemas/dashboard.py backend/tests/schemas/test_dashboard_schemas.py backend/tests/routers/test_dashboard.py
git commit -m "feat(dashboard): accept cloned sankey widgets on the dashboard layout validator"
```

---

## Task 2: Frontend — `cloneWidgetForDashboard` helper

**Files:**
- Create: `frontend/lib/dashboard/clone.ts`
- Test: `frontend/lib/dashboard/__tests__/clone.test.ts` (follow the colocated test convention already used in `lib/`)

**Interfaces:**
- Consumes: `newWidgetId()` from `frontend/components/reports/widgetKit.tsx`; `Widget`, `WidgetGrid` from `frontend/lib/reports/types.ts`.
- Produces: `cloneWidgetForDashboard(source: Widget, existing: Array<Widget | DashboardWidget>): Widget` — used by `AddWidgetMenu` (Task 4) and `CustomDashboard.addClonedWidget` (Task 4).

- [ ] **Step 1: Write the failing test**

```typescript
import { describe, it, expect } from "vitest";
import { cloneWidgetForDashboard } from "../clone";
import type { Widget } from "../../reports/types";

const source: Widget = {
  id: "w_src",
  type: "bar",
  title: "Spend by category",
  grid: { x: 3, y: 2, w: 6, h: 4 },
  // minimal valid bar config for the test
  config: { dataset: "transactions", measure: { agg: "sum", field: "amount" }, dimension: "category" },
} as Widget;

describe("cloneWidgetForDashboard", () => {
  it("gives the clone a fresh id but preserves type/title/config", () => {
    const clone = cloneWidgetForDashboard(source, []);
    expect(clone.id).not.toBe(source.id);
    expect(clone.type).toBe("bar");
    expect(clone.title).toBe("Spend by category");
    expect(clone.config).toEqual(source.config);
  });

  it("deep-copies config (mutating the clone never touches the source)", () => {
    const clone = cloneWidgetForDashboard(source, []);
    (clone.config as Record<string, unknown>).dimension = "merchant";
    expect((source.config as Record<string, unknown>).dimension).toBe("category");
  });

  it("places the clone below all existing widgets, preserving its w/h", () => {
    const existing = [{ id: "a", type: "kpi", title: "x", grid: { x: 0, y: 0, w: 4, h: 3 }, config: {} }] as Widget[];
    const clone = cloneWidgetForDashboard(source, existing);
    expect(clone.grid).toEqual({ x: 0, y: 3, w: 6, h: 4 });
  });

  it("places at row 0 when there are no existing widgets", () => {
    const clone = cloneWidgetForDashboard(source, []);
    expect(clone.grid.y).toBe(0);
  });
});
```

- [ ] **Step 2: Run it — confirm it fails**

Run: `docker compose -p team-phase3 exec frontend npm test -- lib/dashboard/__tests__/clone.test.ts`
Expected: FAIL — cannot find module `../clone`.

- [ ] **Step 3: Implement the helper**

```typescript
// frontend/lib/dashboard/clone.ts
import { newWidgetId } from "../../components/reports/widgetKit";
import type { Widget } from "../reports/types";
import type { DashboardWidget } from "./widget-types";

/**
 * Clone a report widget for placement on the dashboard. The clone is fully
 * independent (deep copy, fresh id) and self-fetches via useReportQuery — no
 * linkage back to the source report. Grid keeps the source w/h and drops to
 * the first free row below every existing widget.
 */
export function cloneWidgetForDashboard(
  source: Widget,
  existing: Array<Widget | DashboardWidget>,
): Widget {
  const copy: Widget = JSON.parse(JSON.stringify(source));
  const maxY = existing.reduce((m, w) => Math.max(m, w.grid.y + w.grid.h), 0);
  copy.id = newWidgetId();
  copy.grid = { x: 0, y: maxY, w: source.grid.w, h: source.grid.h };
  return copy;
}
```

- [ ] **Step 4: Run the test — confirm PASS**

Run: `docker compose -p team-phase3 exec frontend npm test -- lib/dashboard/__tests__/clone.test.ts`
Expected: PASS (all 4).

- [ ] **Step 5: Commit**

```bash
git add frontend/lib/dashboard/clone.ts frontend/lib/dashboard/__tests__/clone.test.ts
git commit -m "feat(dashboard): add cloneWidgetForDashboard helper"
```

---

## Task 3: Frontend — wire the Add-widget picker (re-add `dash_*` tiles) into Customize mode

This builds the missing add-widget UX. CustomDashboard currently has **no** picker or `addWidget` handler (verified). Start with the `dash_*` re-add group; Task 4 adds the "From a report" path to the same menu.

**Files:**
- Create: `frontend/components/dashboard/AddWidgetMenu.tsx`
- Modify: `frontend/components/dashboard/CustomDashboard.tsx`
- Test: `frontend/components/dashboard/__tests__/CustomDashboard.addwidget.test.tsx`

**Interfaces:**
- Consumes: `emptyDashboardWidget(type, id)` + `DashboardWidgetType` from `frontend/lib/dashboard/widget-types.ts`; `newWidgetId()` from widgetKit; the `DashboardWidget` shape.
- Produces:
  - `AddWidgetMenu` props: `{ open: boolean; onClose: () => void; existing: Array<Widget | DashboardWidget>; onAddDashTile: (type: DashboardWidgetType) => void; onAddCloned: (w: Widget) => void }` (the `onAddCloned` path is filled in Task 4; pass a no-op-safe handler now).
  - `CustomDashboard.addDashTile(type)` handler: builds `emptyDashboardWidget`, places at next free row, appends to layout, selects it, marks dirty, closes the menu.

- [ ] **Step 1: Write the failing test**

In `CustomDashboard.addwidget.test.tsx`, render `CustomDashboard` (mock `getDashboard` to resolve a layout missing the `dash_accounts` tile, mock `saveDashboard`), enter Customize, open Add-widget, click the "Accounts" entry, assert a `dash_accounts` widget now renders on the canvas and the Save button is enabled (dirty). Reuse the SWR/provider test harness already used by `dashboard-data-provider.test.tsx`.

```tsx
it("re-adds a removed dash tile from the Add-widget menu", async () => {
  // getDashboard → layout without dash_accounts
  render(<CustomDashboard />, { wrapper: Providers });
  await userEvent.click(await screen.findByRole("button", { name: /customize/i }));
  await userEvent.click(screen.getByRole("button", { name: /add widget/i }));
  await userEvent.click(screen.getByRole("button", { name: /^accounts$/i }));
  expect(await screen.findByTestId("widget-dash_accounts")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /save/i })).toBeEnabled();
});
```

(Match the actual accessible names/test-ids in the codebase — adjust selectors to what `AddWidgetMenu` and the widget shells render. If widgets lack a `data-testid`, add `data-testid={`widget-${w.type}`}` on the dashboard `WidgetShell` wrapper as part of this task.)

- [ ] **Step 2: Run it — confirm it fails**

Run: `docker compose -p team-phase3 exec frontend npm test -- components/dashboard/__tests__/CustomDashboard.addwidget.test.tsx`
Expected: FAIL — no "Add widget" button exists yet.

- [ ] **Step 3: Build `AddWidgetMenu` with the Dashboard group**

Create `AddWidgetMenu.tsx` modeled on `components/reports/WidgetPicker.tsx` (grouped option grid, token classes only). One group for now — **"Dashboard tiles"** — listing the 7 `dash_*` types with human labels (On track, Accounts, Account forecast, Spending, Budget, Forecast by category, Recent transactions). Each entry calls `onAddDashTile(type)`. Leave a placeholder section header "From a report" wired to a disabled/empty state (filled in Task 4). Props per the Interfaces block above.

- [ ] **Step 4: Wire it into CustomDashboard**

In `CustomDashboard.tsx`: add `pickerOpen` state; render an "Add widget" button in the Customize toolbar (only when `editModeActive`); render `<AddWidgetMenu>`; implement `addDashTile`:

```tsx
function addDashTile(type: DashboardWidgetType) {
  const id = newWidgetId();
  const w = emptyDashboardWidget(type, id);
  const maxY = layout.widgets.reduce((m, x) => Math.max(m, x.grid.y + x.grid.h), 0);
  w.grid = { ...w.grid, x: 0, y: maxY };
  setLayout((prev) => ({ ...prev, widgets: [...prev.widgets, w] }));
  setSelectedWidgetId(id);
  setDirty(true);
  setPickerOpen(false);
}
```

- [ ] **Step 5: Run the test — confirm PASS, then full FE verify**

Run: `docker compose -p team-phase3 exec frontend npm test -- components/dashboard/__tests__/CustomDashboard.addwidget.test.tsx`
Then: `docker compose -p team-phase3 exec frontend npx tsc --noEmit && docker compose -p team-phase3 exec frontend npm run lint`
Expected: target test PASS; tsc + lint clean.

- [ ] **Step 6: Commit**

```bash
git add frontend/components/dashboard/AddWidgetMenu.tsx frontend/components/dashboard/CustomDashboard.tsx frontend/components/dashboard/__tests__/CustomDashboard.addwidget.test.tsx
git commit -m "feat(dashboard): add-widget menu re-adds removed dash tiles in customize mode"
```

---

## Task 4: Frontend — "From a report" clone path

**Files:**
- Modify: `frontend/components/dashboard/AddWidgetMenu.tsx`, `frontend/components/dashboard/CustomDashboard.tsx`
- Test: `frontend/components/dashboard/__tests__/AddWidgetMenu.fromreport.test.tsx`

**Interfaces:**
- Consumes: `listReports()` from `frontend/lib/reports/api.ts` (returns `ReportSummary[]`, each carrying full `layout_json.widgets`); `cloneWidgetForDashboard` (Task 2); the `Widget` type.
- Produces: `CustomDashboard.addClonedWidget(source: Widget)` — appends `cloneWidgetForDashboard(source, layout.widgets)`, selects it, marks dirty, closes the menu.

- [ ] **Step 1: Write the failing test**

Mock `listReports` to resolve two reports, one with a `bar` widget and one with a `sankey` widget in `layout_json.widgets`. Render `AddWidgetMenu` open, click "From a report", pick the first report, assert its widgets list shows; click the sankey widget, assert `onAddCloned` is called with a widget whose `type === "sankey"` and a fresh id (`!== source.id`).

```tsx
it("lists a report's widgets and clones the chosen one (incl. sankey)", async () => {
  const onAddCloned = vi.fn();
  render(<AddWidgetMenu open existing={[]} onClose={() => {}} onAddDashTile={() => {}} onAddCloned={onAddCloned} />);
  await userEvent.click(screen.getByRole("button", { name: /from a report/i }));
  await userEvent.click(await screen.findByRole("button", { name: /cash flow report/i }));
  await userEvent.click(await screen.findByRole("button", { name: /cash flow/i }));
  expect(onAddCloned).toHaveBeenCalledTimes(1);
  const arg = onAddCloned.mock.calls[0][0];
  expect(arg.type).toBe("sankey");
  expect(arg.id).not.toBe("w_src_sankey");
});
```

- [ ] **Step 2: Run it — confirm it fails**

Run: `docker compose -p team-phase3 exec frontend npm test -- components/dashboard/__tests__/AddWidgetMenu.fromreport.test.tsx`
Expected: FAIL — no "From a report" path.

- [ ] **Step 3: Implement the from-report sub-flow in `AddWidgetMenu`**

Add internal view state (`"root" | "reports" | "widgets"`). On entering "From a report", `await listReports()` (loading + empty states: "You have no saved reports yet."). Selecting a report shows its `layout_json.widgets` (guard the `Record<string, never>` empty-layout case → "This report has no widgets."). Selecting a widget calls `onAddCloned(cloneWidgetForDashboard(widget, existing))`. Each widget row labels by `widget.title` (fallback to a humanized `widget.type`). Token classes only; keyboard-navigable buttons.

- [ ] **Step 4: Wire `addClonedWidget` into CustomDashboard**

```tsx
function addClonedWidget(source: Widget) {
  const clone = cloneWidgetForDashboard(source, layout.widgets);
  setLayout((prev) => ({ ...prev, widgets: [...prev.widgets, clone] }));
  setSelectedWidgetId(clone.id);
  setDirty(true);
  setPickerOpen(false);
}
```

Pass `onAddCloned={addClonedWidget}` to `AddWidgetMenu`.

- [ ] **Step 5: Add an integration test — cloned widget renders + persists**

In `CustomDashboard.addwidget.test.tsx` (or a sibling), add a test: clone a `bar` widget from a report, assert it renders via the report fall-through (`renderReportWidget`) on the dashboard canvas, click Save, assert `saveDashboard` was called with a `layout_json.widgets` array containing the cloned widget's type. Mock `useReportQuery` so the cloned widget doesn't hit the network.

- [ ] **Step 6: Run the from-report tests + full FE verify**

Run: `docker compose -p team-phase3 exec frontend npm test -- components/dashboard/__tests__/AddWidgetMenu.fromreport.test.tsx components/dashboard/__tests__/CustomDashboard.addwidget.test.tsx`
Then: `docker compose -p team-phase3 exec frontend npx tsc --noEmit && docker compose -p team-phase3 exec frontend npm run lint`
Expected: PASS; tsc + lint clean.

- [ ] **Step 7: Commit**

```bash
git add frontend/components/dashboard/AddWidgetMenu.tsx frontend/components/dashboard/CustomDashboard.tsx frontend/components/dashboard/__tests__/
git commit -m "feat(dashboard): clone a widget from a saved report onto the dashboard"
```

---

## Task 5: Reset-to-default (backend default endpoint + frontend action)

The canonical default lives ONLY in the backend (`DEFAULT_DASHBOARD_LAYOUT`, `routers/dashboard.py:58`). Reset must restore *that* without persisting until the user Saves — so add a read-only endpoint that returns the seed, and have Reset fetch it.

**Files:**
- Modify: `backend/app/routers/dashboard.py` (new `GET /api/v1/dashboard/default`)
- Modify: `frontend/lib/dashboard/api.ts` (new `getDefaultDashboard()`)
- Modify: `frontend/components/dashboard/CustomDashboard.tsx` (Reset action + confirm modal)
- Test: `backend/tests/routers/test_dashboard.py`, `frontend/components/dashboard/__tests__/CustomDashboard.reset.test.tsx`

**Interfaces:**
- Consumes: `DEFAULT_DASHBOARD_LAYOUT` (existing module constant).
- Produces: `GET /api/v1/dashboard/default` → `{ layout_json, canvas_filters_json }` (the seed, no DB write, no auto-create). FE `getDefaultDashboard(): Promise<{ layout_json: LayoutJson; canvas_filters_json: CanvasFilters }>`.

- [ ] **Step 1: Write the failing backend test**

```python
async def test_get_default_returns_seed_without_persisting(client, auth_headers, db):
    r = await client.get("/api/v1/dashboard/default", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    types = [w["type"] for w in body["layout_json"]["widgets"]]
    assert "dash_on_track" in types and len(types) == 7
    # it must NOT have created a row for a user who never GET/PATCHed
    # (assert via a direct count of DashboardLayout for this user == 0)
```

(Use the test file's existing fixtures for an authed user with no prior dashboard row; assert the `DashboardLayout` count stays 0.)

- [ ] **Step 2: Run it — confirm it fails**

Run: `docker compose -p team-phase3 exec backend pytest tests/routers/test_dashboard.py::test_get_default_returns_seed_without_persisting -v`
Expected: FAIL — 404 (route doesn't exist).

- [ ] **Step 3: Add the endpoint**

In `routers/dashboard.py`, add (place ABOVE the `""` GET so the literal path isn't shadowed by a path param — there is none here, but keep it explicit):

```python
@router.get("/default")
async def get_default_dashboard(
    current_user: User = Depends(get_current_user),
):
    """Return the canonical default layout WITHOUT persisting — backs the
    Reset-to-default action. Single source of truth for the seed."""
    return {
        "layout_json": copy.deepcopy(DEFAULT_DASHBOARD_LAYOUT),
        "canvas_filters_json": {},
    }
```

- [ ] **Step 4: Run the backend test — confirm PASS**

Run: `docker compose -p team-phase3 exec backend pytest tests/routers/test_dashboard.py::test_get_default_returns_seed_without_persisting -v`
Expected: PASS.

- [ ] **Step 5: Add `getDefaultDashboard()` to the FE api + failing FE reset test**

Add to `frontend/lib/dashboard/api.ts`:

```typescript
export async function getDefaultDashboard(): Promise<{
  layout_json: LayoutJson;
  canvas_filters_json: CanvasFilters;
}> {
  return apiFetch("/api/v1/dashboard/default");
}
```

Write `CustomDashboard.reset.test.tsx`: render with a customized layout, enter Customize, click "Reset to default", confirm in the modal; mock `getDefaultDashboard` to resolve the 7-tile seed; assert the canvas now shows the 7 default tiles and Save is enabled (dirty, not yet persisted).

- [ ] **Step 6: Implement the Reset action**

In `CustomDashboard.tsx`, add a "Reset to default" button (Customize toolbar) → confirm modal (reuse the app's existing confirm/Dialog primitive; token classes only) → on confirm: `const d = await getDefaultDashboard(); setLayout(d.layout_json); setCanvasFilters(d.canvas_filters_json); setDirty(true);`. Reset does NOT auto-save — the user reviews then Saves (matches Reports' explicit-save model).

- [ ] **Step 7: Run FE reset test + full FE verify**

Run: `docker compose -p team-phase3 exec frontend npm test -- components/dashboard/__tests__/CustomDashboard.reset.test.tsx`
Then: `docker compose -p team-phase3 exec frontend npx tsc --noEmit && docker compose -p team-phase3 exec frontend npm run lint`
Expected: PASS; clean.

- [ ] **Step 8: Commit**

```bash
git add backend/app/routers/dashboard.py backend/tests/routers/test_dashboard.py frontend/lib/dashboard/api.ts frontend/components/dashboard/CustomDashboard.tsx frontend/components/dashboard/__tests__/CustomDashboard.reset.test.tsx
git commit -m "feat(dashboard): reset-to-default via non-persisting default endpoint"
```

---

## Task 6: Mobile read-only stack for `dash_*` widgets

**Files:**
- Modify: `frontend/lib/reports/stack.ts` (`mobileStackHeight`, `orderWidgetsForStack` typing)
- Test: `frontend/lib/reports/__tests__/stack.test.ts` (extend if present; else create)

**Interfaces:**
- Consumes: `DashboardWidget` from `frontend/lib/dashboard/widget-types.ts`.
- Produces: `mobileStackHeight(widget: Widget | DashboardWidget): number | undefined` covering all 7 `dash_*` types; `orderWidgetsForStack` accepts the union.

- [ ] **Step 1: Write the failing test**

```typescript
import { mobileStackHeight } from "../stack";

it("gives each dash_* widget a sensible mobile stack height", () => {
  const h = (type: string) =>
    mobileStackHeight({ id: "x", type, title: "t", grid: { x: 0, y: 0, w: 12, h: 5 }, config: {} } as any);
  expect(h("dash_on_track")).toBeGreaterThanOrEqual(160);
  expect(h("dash_spending")).toBeGreaterThanOrEqual(220); // chart tile
  expect(h("dash_accounts")).toBeGreaterThan(0);
  expect(h("dash_recent_transactions")).toBeGreaterThan(0);
});
```

- [ ] **Step 2: Run it — confirm it fails**

Run: `docker compose -p team-phase3 exec frontend npm test -- lib/reports/__tests__/stack.test.ts`
Expected: FAIL (current `mobileStackHeight` returns `undefined`/throws for `dash_*`).

- [ ] **Step 3: Extend `mobileStackHeight`**

Widen the parameter type to `Widget | DashboardWidget`. Add a `dash_*` branch BEFORE the existing report-type logic. Chart-like dash tiles (`dash_spending`, `dash_budget`, `dash_forecast_category`) reuse the chart formula (`clamp(grid.h*56, 220, 460)`); content tiles (`dash_on_track`, `dash_accounts`, `dash_account_forecast`, `dash_recent_transactions`) get a clamped height so they don't collapse on mobile (e.g. `clamp(grid.h*56, 200, 520)`). Keep the existing report-widget behavior unchanged. Widen `orderWidgetsForStack` to the union (the body already only reads `grid`).

- [ ] **Step 4: Run the test + the existing reports stack tests — confirm no regression**

Run: `docker compose -p team-phase3 exec frontend npm test -- lib/reports/__tests__/stack.test.ts`
Expected: PASS, including any pre-existing report-widget height assertions.

- [ ] **Step 5: Commit**

```bash
git add frontend/lib/reports/stack.ts frontend/lib/reports/__tests__/stack.test.ts
git commit -m "feat(dashboard): mobile read-only stack heights for dash_* widgets"
```

---

## Task 7: Whole-branch verification + review gate

**Files:** none (verification only)

- [ ] **Step 1: Full backend suite**

Run: `docker compose -p team-phase3 exec backend pytest -q`
Expected: green (note the count vs the ~2183 FE / prior BE baselines).

- [ ] **Step 2: Full frontend verify (the complete gate)**

Run: `docker compose -p team-phase3 exec frontend npx tsc --noEmit && docker compose -p team-phase3 exec frontend npm run lint && docker compose -p team-phase3 exec frontend npm test`
Expected: tsc clean, lint clean (esp. `no-explicit-any`), all tests pass.

- [ ] **Step 3: Design-token gate**

Run: `cd frontend && ./scripts/check-design-tokens.sh`
Expected: pass (no raw palette colors introduced by `AddWidgetMenu`/modal).

- [ ] **Step 4: Manual preview (flag forced ON for one org)**

Force `CUSTOM_DASHBOARD` on for a test org via `/system/features` (or an OrgSetting override), load `/dashboard`: remove a tile → re-add it; add a chart AND a sankey from a report → both render/query; Reset → 7 tiles return; Save → reload persists; shrink to 390px → legible read-only stack, no horizontal scroll. Confirm the legacy `/dashboard` is byte-for-byte unchanged with the flag OFF (default).

- [ ] **Step 5: Adversarial review fleet + fold**

Run the project's standard pre-merge review (subagent fleet across faithfulness / React races / a11y+tokens / test-quality → skeptic-verify → synth), fold every actionable finding, re-verify. Then open the PR (flag still OFF; no LegacyDashboard change). **No AI attribution in the PR body.**

---

## Phase 3b (SEPARATE PR — after operator preview, NOT in this plan's PR)

Once Phase 3 is merged and previewed: flip `Feature.CUSTOM_DASHBOARD` default ON, delete `LegacyDashboard` (the verbatim old page body) and the **temporary `DashboardDataProvider` duplication** of legacy data logic, drop the now-dead OFF-path branch in `app/dashboard/page.tsx`, and update tests that asserted the legacy path. Ships as its own small, easily-revertable PR. Tracked in [[project_w4_tierB_customizable_dashboard]].

---

## Self-Review (against the spec)

- **Add from report** → Tasks 2 (clone helper) + 4 (UI flow), incl. Sankey (Task 1 backend + Task 4 picker). ✓
- **Reset to default** → Task 5 (non-persisting default endpoint + confirm-modal action). ✓
- **Mobile read-only pass** → Task 6 (`dash_*` stack heights). ✓
- **Re-add removed tiles from a picker** (Phase 2 acceptance, found unwired) → Task 3. ✓
- **Flag stays OFF / LegacyDashboard untouched this PR** → Global Constraints + Phase 3b split. ✓
- **Sankey on dashboard validator** → Task 1. ✓
- **Type consistency:** `cloneWidgetForDashboard(source, existing)`, `addDashTile`, `addClonedWidget`, `getDefaultDashboard`, `AddWidgetMenu` prop names used identically across Tasks 2-6. ✓
- **Verify includes `npm run lint`** in every FE task + Task 7. ✓
