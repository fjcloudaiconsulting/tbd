# Reports v3 Phase 4a — Widget Editor Popover (TDD Implementation Plan)

Date: 2026-06-12
Author: frontend architect
Status: ready to implement (subagent-driven, single coherent PR)

---

## Goal

Replace the 320 px right-hand `ConfigRail` sidebar with an **anchored floating
popover** that floats *over* the canvas, anchored to the selected widget's DOM
element. The popover must NOT consume flex width, so the canvas no longer
reflows (narrows / re-clamps RGL columns) when a widget is selected. This
reflow is the concrete bug being fixed: today
`app/reports/[id]/page.tsx` mounts `<ConfigRail>` as a flex sibling
(`flex-1` canvas column + `w-80 shrink-0` rail), so selecting a widget
shrinks the canvas from full width and RGL re-clamps to a narrower
breakpoint.

The popover reorganizes the **exact same controls** into **3 tabs
(Data · Filters · Style)** so a long scroll is avoided. This is a
**re-housing + reorganization, not a logic rewrite**: every existing
`onUpdate(nextWidget)` mutation in `ConfigRail` is preserved verbatim by
extracting `ConfigRail`'s sub-blocks into reusable pieces the popover
composes.

## Architecture

```
app/reports/[id]/page.tsx
  editModeActive && selectedWidget
    → renders <WidgetEditorPopover>  (NOT in the flex row; portaled)
    → passes the selected widget's anchor element (a ref/getter) so
      floating-ui can position against it

components/reports/
  WidgetEditorPopover.tsx        NEW — floating-ui shell + 3-tab frame
  config/
    DataTab.tsx                  NEW — source + measure(s) + dimensions
    StyleTab.tsx                 NEW — title + widget-specific knobs + format
    FilterEditor.tsx             NEW — EXTRACTED verbatim from ConfigRail
    MeasuresEditor.tsx           NEW — EXTRACTED verbatim from ConfigRail
    SingleMeasureEditor.tsx      NEW — EXTRACTED verbatim from ConfigRail
    controlConstants.ts          NEW — AGG_OPTIONS / FIELD_OPTIONS / DIMENSION_OPTIONS / helpers
  WidgetShell.tsx                MODIFIED — expose the shell DOM node to the page for anchoring
  Canvas.tsx                     UNCHANGED (must not reflow)
  CanvasFiltersBar.tsx           UNCHANGED (filter model frozen in 4a)
  ConfigRail.tsx                 DELETED (its logic moves into the extracted pieces + tabs)
```

The popover owns: positioning (`useFloating` + `autoUpdate`), dismissal
(`useDismiss` outside-click + Escape), focus management
(`FloatingFocusManager`), the `role`/`aria` contract (`useRole`), tab state,
and which tabs/sub-controls a given widget type shows. It delegates every
*mutation* to the extracted control components, which call the page's
existing `updateWidget` (via the `onUpdate` prop) untouched.

## Tech Stack

- **NEW dep:** `@floating-ui/react` (latest). Confirmed absent today —
  `frontend/package.json` has no `@floating-ui/*`, no `@radix-ui/*`, no
  `@headlessui/*`. Provides `useFloating`, `autoUpdate`, `offset`, `flip`,
  `shift`, `useDismiss`, `useRole`, `useInteractions`, `FloatingPortal`,
  `FloatingFocusManager`.
- Existing: React 19, Next 16 App Router, Tailwind v4 (theme tokens only —
  `No Off-Token` rule), `lucide-react`, vitest 3 + jsdom + Testing Library.
- Test harness: `renderWithSWR` from `tests/utils/render-with-swr.tsx`
  (re-exports `screen`, `fireEvent`, `waitFor`, `within`, `act`).

---

## Non-goals (deferred to Phase 4b — DO NOT do in 4a)

These are explicitly **out of scope** and must be left exactly as today:

1. **Filter model is frozen.** Canvas filters stay `date_range` +
   `account_ids` + `category_ids` (`CanvasFiltersBar.tsx`); per-widget
   overrides stay the full `WidgetFilters` shape; the cascade in
   `lib/reports/resolve.ts` is untouched. Do NOT change `resolveFilters` /
   `isFieldOverridden`.
2. **Do NOT shrink the canvas toolbar to date-only.** `CanvasFiltersBar`
   keeps all three controls.
3. **Do NOT add canvas filter chips.** The "active filter pills" toolbar is 4b.
4. **No source/dataset widening.** The "Data source" select stays a
   *disabled* `Transactions` select exactly as today (enum widening is
   Phase 5).
5. **No auto-save.** Explicit Save semantics (`handleSave`) are preserved.
6. **No new widget knobs** beyond the ones ConfigRail already renders.

4a is **popover + control re-housing + reflow fix ONLY.**

---

## File Structure

### New files

| File | Purpose |
| --- | --- |
| `components/reports/WidgetEditorPopover.tsx` | floating-ui shell, 3-tab frame, a11y wiring, tab-visibility-by-widget-type logic |
| `components/reports/config/controlConstants.ts` | `AGG_OPTIONS`, `AGG_HELP_KEY`, `FIELD_OPTIONS`, `DIMENSION_OPTIONS`, `MAX_SERIES`, `MAX_TABLE_COLUMNS`, `isMultiSeries`, `isSingleAggLocked` — lifted verbatim from `ConfigRail.tsx` |
| `components/reports/config/SingleMeasureEditor.tsx` | extracted `SingleMeasureEditor` (Aggregation + Field selects) |
| `components/reports/config/MeasuresEditor.tsx` | extracted `MeasuresEditor` (multi-series add/remove rows) |
| `components/reports/config/FilterEditor.tsx` | extracted `FilterEditor` + `TxnTypeRadioRow` + `OverridePill` (verbatim) |
| `components/reports/config/DataTab.tsx` | composes source select + measure editor + primary/secondary dimension selects |
| `components/reports/config/StyleTab.tsx` | composes title input + KPI compare / Pie top_n / Area+StackedBar stacked / Line smooth / format select |
| `tests/components/reports/WidgetEditorPopover.test.tsx` | popover behavior: tab switching, per-type tab/control visibility, dismiss/Escape, a11y, anchoring smoke |
| `tests/components/reports/config/FilterEditor.test.tsx` | extracted-filter parity: override pill, all six filter fields wired |
| `tests/components/reports/config/measure-editors.test.tsx` | single + multi-series measure mutation parity |

### Modified files

| File | Change |
| --- | --- |
| `app/reports/[id]/page.tsx` | remove `<ConfigRail>` from the `flex flex-1` row; the canvas column becomes the *only* flex child (no width steal). Mount `<WidgetEditorPopover>` outside the flex row, pass the selected widget's anchor element. Update `import`. |
| `components/reports/WidgetShell.tsx` | accept an optional `onShellRef`/`anchorRef` (or keep the existing `data-widget-shell={id}` attribute as the anchor selector — see Task 5) so the page can resolve the selected widget's DOM node. |
| `package.json` | add `@floating-ui/react` to `dependencies` |
| `package-lock.json` | regenerate (via the frontend container) |
| `tests/app/reports-editor-page.test.tsx` | update every `config-rail` reference (lines ~236-238, ~390-394, ~902). These break and MUST be migrated to the popover testid. Add a **reflow-invariance** test (canvas container width unaffected on open). |
| `ConfigRail.tsx` | **DELETE** after extraction is complete and all references move. |

### ConfigRail control inventory (what must be preserved)

Every control below currently lives in `ConfigRail.tsx` and its
`onUpdate(nextWidget)` logic must be carried over **unchanged**:

| Control | ConfigRail fn | Tab in 4a | Widget types |
| --- | --- | --- | --- |
| Title input | `setTitle` | **Style** | all |
| Data source (disabled `Transactions`) | none (disabled) | **Data** | all |
| Single measure: Aggregation + Field | `setSingleMeasure` via `SingleMeasureEditor` | **Data** | kpi, bar, pie, sparkline |
| Multi-series: per-series label/agg/field + Add/Remove (cap 5; table cap 5 cols) | `setSeries` via `MeasuresEditor` | **Data** | line, area, stacked_bar, table |
| Primary dimension select | `setPrimaryDimension` | **Data** | all except kpi |
| Secondary dimension select ("Break down by" / "Secondary dimension") | `setSecondaryDimension` | **Data** | bar, table only |
| KPI "Compare to prior period" checkbox | `setComparePrior` | **Style** | kpi |
| Pie "Top N slices" number | `setTopN` | **Style** | pie |
| Area/StackedBar "Stack series" checkbox | `setStacked` | **Style** | area, stacked_bar |
| FilterEditor (date/accounts/categories/txn_type/amount_range/tags + override pills) | `setFilters` | **Filters** | all |

> **Deviation flagged (read the Self-Review + final report):** ConfigRail does
> **NOT** currently render a `Line.smooth` toggle nor a `format` select, even
> though both exist in the type model (`LineConfig.smooth`,
> `*.format`). The instructions list "Line smooth" and "format" as Style knobs.
> **Decision:** to keep 4a a strict re-housing with zero new behavior, port
> ONLY the controls ConfigRail renders today. `Line.smooth` and `format`
> selects are **NOT added in 4a** (they would be new logic / new knobs, which
> the non-goals forbid). They are noted as a fast-follow. The Style tab is
> still correct: it shows title + whatever per-type knob(s) ConfigRail had.

---

## Floating-UI wiring (the genuinely new/tricky part)

`WidgetEditorPopover` props:

```ts
interface Props {
  widget: Widget;
  canvasFilters: CanvasFilters;
  anchorEl: HTMLElement | null;   // the selected widget's shell DOM node
  onUpdate: (next: Widget) => void;
  onClose: () => void;
}
```

Core hook setup:

```tsx
import {
  useFloating, autoUpdate, offset, flip, shift,
  useDismiss, useRole, useInteractions,
  FloatingPortal, FloatingFocusManager,
} from "@floating-ui/react";

const { refs, floatingStyles, context } = useFloating({
  open: true,                     // mounted only while a widget is selected
  onOpenChange: (next) => { if (!next) onClose(); },
  placement: "right-start",       // flips to left/bottom via middleware when clipped
  middleware: [offset(8), flip({ padding: 8 }), shift({ padding: 8 })],
  whileElementsMounted: autoUpdate, // reposition on scroll/resize/widget-move
});

// Anchor to the selected widget element (external reference element).
useEffect(() => {
  refs.setReference(anchorEl);
}, [anchorEl, refs]);

const dismiss = useDismiss(context, {
  outsidePress: true,             // close on outside-click
  escapeKey: true,                // close on Escape
});
const role = useRole(context, { role: "dialog" });
const { getFloatingProps } = useInteractions([dismiss, role]);
```

Render with portal + focus trap:

```tsx
return (
  <FloatingPortal>
    <FloatingFocusManager context={context} modal={false} initialFocus={-1}>
      <div
        ref={refs.setFloating}
        style={floatingStyles}
        {...getFloatingProps()}
        data-testid="widget-editor-popover"
        role="dialog"
        aria-label="Widget settings"
        className="z-50 flex max-h-[80vh] w-80 flex-col overflow-hidden rounded-lg border border-border bg-surface shadow-lg"
      >
        {/* header (title + Close), tablist, active tab panel */}
      </div>
    </FloatingFocusManager>
  </FloatingPortal>
);
```

Key points for the implementer:

- **`modal={false}`** so the popover doesn't trap the whole page (the user can
  still see/scroll the canvas behind it). Focus is still *managed* — initial
  focus and Escape/return work — without a hard modal trap, matching the
  non-blocking menu pattern in `OverflowMenu.tsx`.
- **Anchor via `refs.setReference(anchorEl)`**, NOT a trigger ref. The
  reference element is the *selected widget shell*, resolved by the page (see
  Task 5). When `anchorEl` is `null` the popover should not render (page
  already gates on `selectedWidget`, but guard anyway).
- **`autoUpdate`** (via `whileElementsMounted`) keeps the popover glued to the
  widget as the user scrolls the canvas, resizes the window, or drags the
  widget. This is what makes "anchored, floats over" feel correct.
- **No canvas reflow:** the popover is in a `FloatingPortal` (renders at the
  document body, `position: fixed`/absolute via `floatingStyles`), so it is
  never a flex sibling of the canvas. This is the structural fix.

### jsdom test handling for floating-ui

floating-ui calls `getBoundingClientRect` / `ResizeObserver` /
`requestAnimationFrame`; jsdom returns zeros and lacks `ResizeObserver`.
Strategy (mirrors how `Canvas` is stubbed in the existing editor test):

- In `WidgetEditorPopover.test.tsx`, provide a `ResizeObserver` polyfill in a
  `beforeEach` (`global.ResizeObserver = class { observe(){} unobserve(){}
  disconnect(){} }`). jsdom 26 has no `ResizeObserver`.
- Do **not** assert pixel coordinates in jsdom (they'll be 0). Assert
  structure/behavior instead: the popover renders, the right tab panel shows,
  Escape closes, outside-press closes, `role="dialog"` + `aria-label` present,
  tab `aria-selected` toggles.
- `FloatingPortal` renders into `document.body`; query with `screen.*` (which
  searches the whole document), not `within(container)`.
- For the **page-level reflow test**, stub the popover's positioning is not
  needed — assert the *canvas container* width/structure is unchanged. Since
  the editor test already mocks `@/components/reports/Canvas`, the cleanest
  assertion is: the canvas column is the sole flex child and no `w-80` rail
  sibling exists when the popover is open (see Task 8).

---

## TDD Task Breakdown

> **Single PR.** 4a is large but cohesive; it lands as ONE PR. Internal
> ordering below keeps each step independently testable. Run the **full**
> vitest suite (not single files) before opening the PR (project rule —
> see `reference_frontend_full_suite_verification.md`).
>
> Commands (run inside the frontend container; for parallel-agent sessions use
> an isolated compose project per CLAUDE.md):
> - Single file: `docker compose exec -T frontend npx vitest run <path>`
> - Full suite: `docker compose exec -T frontend npx vitest run`
> - Type-check: `docker compose exec -T frontend npx tsc --noEmit`

Commit after each task (no AI attribution in messages per user rule).

---

### Task 1 — Add the `@floating-ui/react` dependency

**Files:** `package.json`, `package-lock.json`

No test (dependency-only). Verification is install + import resolves.

1. Add `"@floating-ui/react": "^0.27.0"` (or current latest) to
   `dependencies` in `package.json`.
2. Regenerate the lockfile **inside the container** so the platform-correct
   tree lands:
   `docker compose exec -T frontend npm install`
   (this updates `package-lock.json`; commit both files).
3. **Rebuild note:** the dev image COPYs `package.json`/lockfile but a running
   container won't have the new dep until rebuilt:
   `docker compose up --build -d frontend`. (See
   `reference_dockerfile_dev_install.md` / dev-unmounted-files gotcha — a
   stale image will fail `import` resolution locally while CI is green.)
4. Verify: `docker compose exec -T frontend node -e "require.resolve('@floating-ui/react')"`.

**Commit:** `build(reports): add @floating-ui/react for widget editor popover`

---

### Task 2 — Extract control constants + helpers (no behavior change)

**Files:** `components/reports/config/controlConstants.ts` (new)

**Failing test:** none required (pure constant move); covered transitively by
Task 3/4 tests. Optionally add a trivial `tests/components/reports/config/controlConstants.test.ts`
asserting `DIMENSION_OPTIONS.length === 9` and `AGG_OPTIONS.length === 4` to
lock the move.

**Implement:** copy verbatim from `ConfigRail.tsx`:
`AGG_OPTIONS`, `AGG_HELP_KEY`, `FIELD_OPTIONS` (built from
`MEASURE_FIELD_LABELS` in `lib/reports/series.ts`), `DIMENSION_OPTIONS`,
`MAX_SERIES`, `MAX_TABLE_COLUMNS`, `isMultiSeries`, `isSingleAggLocked`.
Export each. Keep the `HelpTooltipKey` typing.

**Run-to-pass:** `docker compose exec -T frontend npx tsc --noEmit`

**Commit:** `refactor(reports): extract widget-config control constants`

---

### Task 3 — Extract measure editors (single + multi-series)

**Files:** `components/reports/config/SingleMeasureEditor.tsx`,
`components/reports/config/MeasuresEditor.tsx` (both new),
`tests/components/reports/config/measure-editors.test.tsx` (new)

**Failing test first** (`measure-editors.test.tsx`):
- `SingleMeasureEditor`: rendering a measure `{agg:"sum",field:"amount"}`,
  changing the **Aggregation** select to `count` calls `onChange` with
  `{agg:"count",field:"amount"}`; changing **Field** to `id` calls with
  `{agg:"sum",field:"id"}`. Query by the existing `aria-label="Aggregation"` /
  `"Field"`.
- `MeasuresEditor`: with a `line` widget (2 series), `data-testid="measure-add"`
  appends a `{measure:{agg:"sum",field:"amount"}}` row; `measure-remove-1`
  removes index 1; remove is hidden when only one series; cap respected at
  `MAX_SERIES`; a `table` widget caps at `MAX_TABLE_COLUMNS` and labels rows
  "Column N".

Run-to-fail: `docker compose exec -T frontend npx vitest run tests/components/reports/config/measure-editors.test.tsx`

**Implement:** move `SingleMeasureEditor` and `MeasuresEditor` verbatim from
`ConfigRail.tsx` into their own files, importing from `controlConstants.ts`
and reusing the same `Section`/`HelpTooltip` markup. Keep all `data-testid`s
(`measure-row-N`, `measure-add`, `measure-remove-N`) and `aria-label`s
identical — downstream tests and parity depend on them.

Run-to-pass: same vitest path, then `npx tsc --noEmit`.

**Commit:** `refactor(reports): extract single/multi-series measure editors`

---

### Task 4 — Extract FilterEditor verbatim

**Files:** `components/reports/config/FilterEditor.tsx` (new — includes
`FilterEditor`, `TxnTypeRadioRow`, `OverridePill`),
`tests/components/reports/config/FilterEditor.test.tsx` (new)

**Failing test first** (`FilterEditor.test.tsx`):
- Renders all six fields: Date range (`DatePresetChips`), Accounts
  (`AccountFilter`), Categories (`CategoryPicker`), Transaction type radios,
  Amount min/max (`aria-label="Widget amount min"` / `"...max"`), Tags
  (`TagFilter`).
- **Override pill parity:** given widget `filters.date_range` differing from
  `canvasFilters.date_range`, `data-testid="override-pill"` renders next to the
  Date range label (drives through `isFieldOverridden` from `resolve.ts` —
  do NOT reimplement it).
- Changing amount min calls `onChange` with the merged `amount_range`.

> Mock `AccountFilter`/`CategoryPicker`/`TagFilter`/`DatePresetChips` if they
> fetch on mount (AccountFilter/CategoryPicker hit the org API). The existing
> editor test renders the full page without mocking these, so they're
> jsdom-safe; prefer rendering them real first and only mock if a fetch throws.

Run-to-fail: `docker compose exec -T frontend npx vitest run tests/components/reports/config/FilterEditor.test.tsx`

**Implement:** move `FilterEditor`, `TxnTypeRadioRow`, `OverridePill` verbatim.
Keep `isFieldOverridden` import from `@/lib/reports/resolve`. No logic change.

Run-to-pass + `tsc --noEmit`.

**Commit:** `refactor(reports): extract per-widget FilterEditor`

---

### Task 5 — Expose the widget shell DOM node for anchoring

**Files:** `components/reports/WidgetShell.tsx`, `app/reports/[id]/page.tsx`
(read path only here), test added in Task 8.

**Decision (anchor mechanism):** `WidgetShell` already renders
`data-widget-shell={widgetId}` on its root `div`. The simplest, lowest-risk
anchor is to resolve the selected widget's node by that attribute in the page:

```ts
const anchorEl = selectedWidgetId
  ? (document.querySelector(`[data-widget-shell="${selectedWidgetId}"]`) as HTMLElement | null)
  : null;
```

Compute it in an effect/state so it updates after the widget mounts and after
re-selection. To make this robust against re-render timing, store it in state
set from a `useEffect` keyed on `selectedWidgetId` + `layout.widgets`:

```ts
const [anchorEl, setAnchorEl] = useState<HTMLElement | null>(null);
useEffect(() => {
  if (!editModeActive || !selectedWidgetId) { setAnchorEl(null); return; }
  setAnchorEl(
    document.querySelector(`[data-widget-shell="${selectedWidgetId}"]`) as HTMLElement | null,
  );
}, [editModeActive, selectedWidgetId, layout.widgets]);
```

> Rationale: avoids threading a ref callback through `Canvas`'s
> `renderWidget` → `WidgetShell` plumbing (which would touch the frozen
> `Canvas` contract). `data-widget-shell` already exists for exactly this
> kind of lookup. If a later phase needs sub-pixel precision, switch to a ref
> callback on `WidgetShell` then; for 4a the attribute query is sufficient and
> keeps `Canvas` untouched.

**WidgetShell change:** add `aria-expanded` + `aria-haspopup="dialog"` to the
selectable root (or to a future explicit trigger) so the selected widget
announces it controls a dialog (a11y must-flag #5). Set
`aria-expanded={selected}` on the shell root and keep `onClick={onSelect}`.

No standalone test here (covered by Task 8). `tsc --noEmit` must pass.

**Commit:** `feat(reports): expose widget shell node + aria for popover anchor`

---

### Task 6 — Data + Style tab composers

**Files:** `components/reports/config/DataTab.tsx`,
`components/reports/config/StyleTab.tsx` (new). Tested via the popover test
(Task 7), but a focused tab unit test is encouraged.

**Implement `DataTab`** — composes, using `widget` + `onUpdate`:
- Data source: disabled `Transactions` select (verbatim from ConfigRail).
- Measures: `isMultiSeries(widget) ? <MeasuresEditor> : <SingleMeasureEditor>`
  wired to `setSeries` / `setSingleMeasure` (port these mutation fns from
  ConfigRail into `DataTab` or a shared `useWidgetMutations(widget,onUpdate)`
  helper — see note below).
- Primary dimension select (hidden for `kpi`), `setPrimaryDimension`.
- Secondary dimension select (only `bar`/`table`), `setSecondaryDimension`.

**Implement `StyleTab`** — composes:
- Title input (`setTitle`).
- KPI: compare_prior_period checkbox (`setComparePrior`).
- Pie: top_n number (`setTopN`).
- Area/StackedBar: stacked checkbox (`setStacked`).
- (NOT Line.smooth / format — see deviation note above.)

> **Mutation-fn placement:** the `setTitle/setSingleMeasure/setSeries/
> setPrimaryDimension/setSecondaryDimension/setComparePrior/setTopN/setStacked/
> setFilters` closures are currently local to `ConfigRail`. Port them into a
> small shared hook `useWidgetMutations(widget, onUpdate)` in
> `components/reports/config/useWidgetMutations.ts` (NEW, optional 9th file)
> OR inline them per-tab. Hook is preferred: one home, identical logic, both
> tabs + the filter tab consume it. Either way the bodies are **copied
> verbatim** — no logic change.

`tsc --noEmit` must pass.

**Commit:** `feat(reports): add Data and Style tab composers for widget editor`

---

### Task 7 — WidgetEditorPopover shell (floating-ui + tabs + a11y)

**Files:** `components/reports/WidgetEditorPopover.tsx` (new),
`tests/components/reports/WidgetEditorPopover.test.tsx` (new)

**Failing test first** (`WidgetEditorPopover.test.tsx`), with the
`ResizeObserver` polyfill in `beforeEach`:

1. Renders `data-testid="widget-editor-popover"` with `role="dialog"` and
   `aria-label="Widget settings"` when given a non-null `anchorEl`
   (use `document.createElement("div")` appended to body as the anchor).
2. **Default tab = Data:** the Data panel (data-source select + measure
   controls) is visible; Filters/Style panels are not.
3. **Tablist a11y:** three tabs with `role="tab"`, the active tab has
   `aria-selected="true"`, tabpanels labelled via `aria-labelledby`. Clicking
   "Filters" shows the FilterEditor; clicking "Style" shows the title input.
4. **Per-type visibility:**
   - `kpi`: Data tab shows measure + source but **no** primary/secondary
     dimension; Style shows compare checkbox.
   - `pie`/`sparkline`: single measure, primary dimension present, **no**
     secondary dimension; Style shows top_n only for pie.
   - `bar`: primary + secondary ("Break down by") dimension; Style has none of
     the type knobs.
   - `line`/`area`/`stacked_bar`/`table`: `MeasuresEditor` (add/remove);
     `table`+`bar` show secondary dimension; area/stacked_bar show "Stack
     series" in Style.
5. **Dismiss:** pressing `Escape` calls `onClose`; clicking outside the
   popover (mousedown on `document.body`) calls `onClose`.
6. **aria-expanded** is asserted at the page level (Task 8), not here.

Run-to-fail: `docker compose exec -T frontend npx vitest run tests/components/reports/WidgetEditorPopover.test.tsx`

**Implement:** the floating-ui shell exactly as in the "Floating-UI wiring"
section. Header: `<h2>Widget settings</h2>` + a `Close` button
(`onClick={onClose}`) mirroring ConfigRail's header. Tablist:

```tsx
const [tab, setTab] = useState<"data" | "filters" | "style">("data");
// role="tablist" with three role="tab" buttons; arrow-key roving optional
// for 4a (Tab/click is enough), but wire aria-selected + aria-controls +
// id/aria-labelledby on each tab/panel pair.
```

Tab panels:
- Data → `<DataTab widget onUpdate />`
- Filters → `<FilterEditor filters={widget.config.filters ?? {}} canvasFilters onChange={setFilters} />`
- Style → `<StyleTab widget onUpdate />`

Per-type sub-control visibility lives inside `DataTab`/`StyleTab` (they already
branch on `widget.type`); the popover just always renders all three tabs (each
tab is meaningful for every type: Data always has source+measure, Filters
always applies, Style always has title).

Run-to-pass + `tsc --noEmit`.

**Commit:** `feat(reports): widget editor popover shell with tabs and a11y`

---

### Task 8 — Wire the popover into the page + delete ConfigRail + reflow test

**Files:** `app/reports/[id]/page.tsx`,
`tests/app/reports-editor-page.test.tsx`, delete `ConfigRail.tsx`.

**Failing tests first** (update + add in `reports-editor-page.test.tsx`):

a. **Migrate the three `config-rail` references** to
   `widget-editor-popover`:
   - "adds a KPI widget…" (~line 236): after adding a KPI it's selected →
     assert `screen.getByTestId("widget-editor-popover")` instead of
     `config-rail`.
   - "shows the 'Overrides canvas' pill…" (~line 390): after selecting the
     widget, the popover mounts; **switch to the Filters tab** before asserting
     `override-pill` (the pill now lives in the Filters tab, not an
     always-visible rail). Add `fireEvent.click(screen.getByRole("tab",
     {name:/filters/i}))`.
   - "after a successful save lands on the read-only view…" (~line 902):
     `queryByTestId("config-rail")` → `queryByTestId("widget-editor-popover")`
     must be null in view mode.

b. **NEW reflow-invariance test** — "opening the widget editor popover does
   not reflow the canvas":
   - Load `REPORT_WITH_WIDGET`, enter edit mode, capture the canvas column.
     Because the canvas is mocked (no real width), assert *structure* not
     pixels: the flex row (`report-editor`'s inner `flex flex-1` container) has
     exactly **one** flex child (the canvas column) both before and after the
     widget is selected — i.e. selecting a widget does NOT insert a `w-80`
     sibling. Concretely:
     - Give the canvas column a stable testid (add `data-testid="report-canvas-column"`
       to the `flex-1` div in `page.tsx`).
     - Before select: `report-canvas-column` is the only element-child of the
       flex row.
     - Click the widget → `widget-editor-popover` appears (in the portal,
       i.e. NOT inside `report-canvas-column`).
     - After select: `report-canvas-column` is **still** the only
       element-child of the flex row (the popover is portaled to body, not a
       sibling). This is the regression guard for the reflow bug.

Run-to-fail: `docker compose exec -T frontend npx vitest run tests/app/reports-editor-page.test.tsx`

**Implement `page.tsx` changes:**
1. Replace `import ConfigRail` with
   `import WidgetEditorPopover from "@/components/reports/WidgetEditorPopover";`
2. Add the `anchorEl` state + effect from Task 5.
3. In the `flex flex-1 overflow-hidden` row: keep ONLY the canvas column
   (`flex-1 overflow-y-auto …`), add `data-testid="report-canvas-column"`.
   **Remove the `{editModeActive && selectedWidget && (<ConfigRail …/>)}`
   block from inside the flex row.**
4. After the flex row (sibling, still inside `report-editor`), mount:
   ```tsx
   {editModeActive && selectedWidget && anchorEl && (
     <WidgetEditorPopover
       widget={selectedWidget}
       canvasFilters={canvasFilters}
       anchorEl={anchorEl}
       onUpdate={updateWidget}
       onClose={() => setSelectedWidgetId(null)}
     />
   )}
   ```
5. Delete `components/reports/ConfigRail.tsx`.

Run-to-pass on the page test, then `tsc --noEmit`.

**Commit:** `feat(reports): mount widget editor popover, remove ConfigRail rail`

---

### Task 9 — Full-suite green + type-check + cleanup

**Files:** none (verification gate).

1. `docker compose exec -T frontend npx tsc --noEmit` → clean.
2. `docker compose exec -T frontend npx vitest run` → **entire** suite green
   (project rule: never trust single-file runs; a cross-file rename can pass
   the touched file and break another — see
   `reference_frontend_full_suite_verification.md`).
3. Grep for stragglers:
   `grep -rn "config-rail\|ConfigRail" frontend/` → should return zero hits
   (component deleted, all testids migrated).
4. Confirm `Canvas.tsx` and `CanvasFiltersBar.tsx` are untouched in the diff
   (`git diff --stat`).

**Commit (if any cleanup):** fold into the prior commit or
`test(reports): migrate editor tests off ConfigRail to popover`.

---

## Must-flag list (implementer: handle each explicitly)

1. **`@floating-ui/react` dep + lockfile via the container.** Add to
   `package.json`, run `npm install` **inside** the frontend container so the
   lockfile matches the container platform, then `up --build -d frontend` —
   a running dev container will otherwise fail to resolve the new import even
   though CI (fresh build) is green (dev-unmounted/stale-image gotcha).
2. **Existing `config-rail` testid references break.** Three spots in
   `tests/app/reports-editor-page.test.tsx` (~236, ~390, ~902) reference
   `data-testid="config-rail"`. All must move to `widget-editor-popover`, and
   the override-pill test must first click the **Filters tab** (the pill is no
   longer always-visible).
3. **Page flex-container change.** Remove `<ConfigRail>` from the
   `flex flex-1 overflow-hidden` row so the canvas column is the sole flex
   child; the popover is portaled, never a flex sibling. Tag the canvas column
   `data-testid="report-canvas-column"` for the reflow test.
4. **Tab state + per-type control visibility.** Default tab = Data. Tabs are
   always all three. Within Data/Style, branch on `widget.type`:
   - `kpi`: no primary/secondary dimension (Data); compare checkbox (Style).
   - `pie`/`sparkline`: single measure + single primary dimension, **no**
     secondary dimension; pie also gets top_n (Style); sparkline gets nothing
     extra in Style.
   - `bar`: primary + secondary ("Break down by"); no Style knob.
   - `table`: multi-series (columns, cap 5) + primary + secondary.
   - `line`/`area`/`stacked_bar`: multi-series (cap 5); area/stacked_bar get
     "Stack series" (Style); stacked_bar's checkbox defaults checked
     (`stacked !== false`).
5. **A11y:** `FloatingFocusManager` manages focus (modal={false},
   `initialFocus={-1}` so focus lands on the dialog, not the first input);
   Escape + outside-press close via `useDismiss`; `role="dialog"` +
   `aria-label` via `useRole`; the selected widget shell gets
   `aria-haspopup="dialog"` + `aria-expanded={selected}`; the tablist uses
   `role="tablist"`/`role="tab"`/`role="tabpanel"` with
   `aria-selected`/`aria-controls`/`aria-labelledby`. Touch-target floor for
   the Close + tab buttons per DESIGN.md.
6. **Reflow invariance test (the core regression guard):** assert the canvas
   column remains the **sole element-child** of the flex row when the popover
   opens (popover is portaled to body), so canvas geometry can't change on
   select. This is the test that proves the bug is fixed.
7. **`No Off-Token` design rule:** every color must come from theme tokens
   (`border-border`, `bg-surface`, `text-text-*`, `ring-accent`, etc.) — reuse
   ConfigRail's exact classes. `frontend/scripts/check-design-tokens.sh`
   CI-blocks raw Tailwind palette colors.
8. **Frozen surfaces:** do NOT edit `Canvas.tsx`, `CanvasFiltersBar.tsx`,
   `lib/reports/resolve.ts`, or the filter model. Confirm via `git diff --stat`.

---

## Self-Review

**Spec coverage:**
- Popover replaces ConfigRail, floats over canvas, anchored to widget via
  `data-widget-shell` lookup → Tasks 5, 7, 8. ✔
- floating-ui `useFloating`+`autoUpdate`+`useDismiss`+`useRole`+
  `FloatingFocusManager` wiring spelled out → "Floating-UI wiring". ✔
- 3 tabs Data/Filters/Style, default Data → Task 7. ✔
- Data tab = disabled source + measure(s) + primary/optional-secondary
  dimension; Filters = verbatim FilterEditor; Style = title + per-type knobs →
  Tasks 4, 6. ✔
- ALL ConfigRail controls + their `onUpdate` logic preserved by extraction →
  Tasks 2-4, 6 (verbatim ports). ✔
- Canvas does not reflow; ConfigRail removed from flex row; popover portaled →
  Task 8 + reflow test. ✔
- Page still gates on `editModeActive && selectedWidget` → Task 8 mount guard. ✔
- Real test commands + full-suite gate → every task + Task 9. ✔
- Must-flag list (8 items incl. dep/lockfile, broken testids, flex change, tab
  state, a11y, reflow test) → present. ✔
- Single coherent PR with internal ordering → stated; 9 tasks. ✔

**Placeholder scan:** no `TODO`/`FIXME`/`...`-as-stub left in the plan; every
new file has a concrete purpose and at least one test path. The optional
`useWidgetMutations.ts` is called out as optional, not a dangling reference.

**Type consistency:** all types referenced (`Widget`, `CanvasFilters`,
`WidgetFilters`, `Measure`, `SeriesConfig`, `Dimension`, `KPIConfig`/
`BarConfig`/`PieConfig`/`SparklineConfig`/`LineConfig`/`AreaConfig`/
`StackedBarConfig`/`TableConfig`) exist in `lib/reports/types.ts` as read.
`anchorEl: HTMLElement | null` matches floating-ui's external-reference
contract. `useReportQuery`/SWR untouched. `tsc --noEmit` is a gate in Tasks
2-9.

**Known deviation (also in the final report):** ConfigRail renders **no**
`Line.smooth` and **no** `format` control today; the brief listed both as Style
knobs. To honor "re-housing, not logic rewrite" + the no-new-knobs non-goal,
4a ports only existing controls and defers `smooth`/`format` to a fast-follow.
