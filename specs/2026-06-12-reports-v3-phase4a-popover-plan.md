# Reports v3 Phase 4a — Widget Editor Popover (TDD Implementation Plan)

Date: 2026-06-12
Author: frontend architect
Status: ready to implement (subagent-driven, **two-PR split** per architect review)

---

## Goal

Replace the 320 px right-hand `ConfigRail` sidebar with an **anchored floating
popover** that floats *over* the canvas, anchored to the selected widget's DOM
element. The popover must NOT consume flex width, so the canvas no longer
reflows (narrows / re-clamps RGL columns) when a widget is selected. This
reflow is the concrete bug being fixed: today **both** report editor pages
mount `<ConfigRail>` as a flex sibling inside an identical
`flex flex-1 overflow-hidden` row (`flex-1` canvas column + ConfigRail's
`w-80 shrink-0` rail), so selecting a widget shrinks the canvas from full
width and RGL re-clamps to a narrower breakpoint:

- `app/reports/[id]/page.tsx` — saved-report editor. Flex row ~line 751;
  `<ConfigRail>` rendered ~822-829 gated on `editModeActive && selectedWidget`.
- `app/reports/new/page.tsx` — unsaved-draft editor. Flex row ~line 266;
  `<ConfigRail>` rendered ~310-317 gated on `selectedWidget` **only** (the
  draft page is always in edit mode, there is no `editModeActive` flag).

**Both pages have the identical reflow bug and both must get the identical
migration.** Deleting `ConfigRail` without migrating `new/page.tsx` breaks the
build.

The popover reorganizes the **exact same controls** into **3 tabs
(Data · Filters · Style)** so a long scroll is avoided. This is a
**re-housing + reorganization, not a logic rewrite**: every existing
`onUpdate(nextWidget)` mutation in `ConfigRail` is preserved verbatim by
extracting `ConfigRail`'s sub-blocks into reusable pieces the popover
composes.

## Architecture

```
app/reports/[id]/page.tsx   (editModeActive && selectedWidget)
app/reports/new/page.tsx    (selectedWidget)
  → each resolves the selected widget's shell DOM node into `anchorEl`
    state via a useEffect keyed on the selected-widget id (the node is
    found by the existing `data-widget-shell={id}` attribute)
  → renders <WidgetEditorPopover anchorEl=…>  (NOT in the flex row; portaled)

components/reports/
  WidgetEditorPopover.tsx        NEW (PR1, built+unit-tested; wired in PR2)
  config/
    controlConstants.ts          NEW (PR1) — AGG_OPTIONS / FIELD_OPTIONS / DIMENSION_OPTIONS / caps / helpers
    SingleMeasureEditor.tsx      NEW (PR1) — EXTRACTED verbatim from ConfigRail
    MeasuresEditor.tsx           NEW (PR1) — EXTRACTED verbatim from ConfigRail
    FilterEditor.tsx             NEW (PR1) — EXTRACTED verbatim (FilterEditor + TxnTypeRadioRow + OverridePill + Section)
    useWidgetMutations.ts        NEW (PR1) — EXTRACTED verbatim mutation closures
    DataTab.tsx                  NEW (PR1) — source + measure(s) + dimensions
    StyleTab.tsx                 NEW (PR1) — title + widget-specific knobs
  ConfigRail.tsx                 PR1: KEEPS RENDERING the extracted pieces (zero behavior change)
                                 PR2: DELETED
  WidgetShell.tsx                PR2 — add aria-haspopup/aria-expanded; node already carries data-widget-shell
  Canvas.tsx                     UNCHANGED (must not reflow)
  CanvasFiltersBar.tsx           UNCHANGED (filter model frozen in 4a)
```

The popover owns: positioning (`useFloating` + `autoUpdate`), dismissal
(`useDismiss` outside-press + Escape), focus management
(`FloatingFocusManager`), the `role`/`aria` contract (`useRole`), tab state,
and which tabs/sub-controls a given widget type shows. It delegates every
*mutation* to the extracted control components, which call the page's existing
`updateWidget` (via the `onUpdate` prop) untouched.

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
- Vitest setup: `frontend/vitest.setup.ts` (registered in `vitest.config.ts`
  `setupFiles`). It currently has **no `ResizeObserver` polyfill** — PR1 adds
  it globally here (see Task 1b).

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
4. **No source/dataset widening.** The "Data source" select stays a *disabled*
   `Transactions` select exactly as today (enum widening is Phase 5).
5. **No auto-save.** Explicit Save semantics are preserved on both pages.
6. **No new widget knobs** beyond the ones ConfigRail already renders. In
   particular `Line.smooth` and a `format` select are **NOT added** — see the
   "Known deviation" note in Self-Review.

4a is **popover + control re-housing + reflow fix ONLY.**

---

## ConfigRail control inventory (what must be preserved verbatim)

Every control below currently lives in `ConfigRail.tsx`; its
`onUpdate(nextWidget)` logic must be carried over **unchanged**. Line numbers
reference the current `ConfigRail.tsx`.

| Control | ConfigRail fn | Tab in 4a | Widget types |
| --- | --- | --- | --- |
| Title input | `setTitle` (122-124) | **Style** | all |
| Data source (disabled `Transactions`) | none (disabled, 256-265) | **Data** | all |
| Single measure: Aggregation + Field | `setSingleMeasure` (134-144) via `SingleMeasureEditor` (431-474) | **Data** | kpi, bar, pie, sparkline |
| Multi-series rows + Add/Remove (cap 5; table cap 5 cols) | `setSeries` (146-153) via `MeasuresEditor` (476-598) | **Data** | line, area, stacked_bar, table |
| Primary dimension select | `setPrimaryDimension` (155-172) | **Data** | all except kpi |
| Secondary dimension select | `setSecondaryDimension` (174-193) | **Data** | bar, table only |
| KPI "Compare to prior period" checkbox | `setComparePrior` (195-205) | **Style** | kpi |
| Pie "Top N slices" number | `setTopN` (207-214) | **Style** | pie |
| Area/StackedBar stacked checkbox | `setStacked` (216-226) | **Style** | area, stacked_bar |
| FilterEditor (date/accounts/categories/txn_type/amount_range/tags + override pills) | `setFilters` (126-132) via `FilterEditor` (600-731) | **Filters** | all |

### Verbatim branches that MUST be named explicitly (transcription-bug hotspots)

These are the two spots where a silent copy error would hide. Move them
**character-for-character**:

1. **Measure branch (ConfigRail ~270-283)** — into `DataTab`:
   ```tsx
   {isMultiSeries(widget) ? (
     <MeasuresEditor widget={widget} onChange={setSeries} />
   ) : (
     <SingleMeasureEditor
       measure={
         (widget.config as KPIConfig | BarConfig | PieConfig | SparklineConfig)
           .measure
       }
       onChange={setSingleMeasure}
     />
   )}
   ```
   The `(widget.config as KPIConfig | BarConfig | PieConfig | SparklineConfig).measure`
   cast-and-extract is the load-bearing part — keep the union and the `.measure`
   access exactly.

2. **Stacked Style branch (ConfigRail ~372-388)** — into `StyleTab`. BOTH the
   label branch AND the default branch differ between `area` and `stacked_bar`
   and must be preserved verbatim:
   ```tsx
   {(widget.type === "area" || widget.type === "stacked_bar") && (
     <Section label={widget.type === "stacked_bar" ? "Stack mode" : "Stack series"}>
       <label ...>
         <input
           type="checkbox"
           checked={
             widget.type === "stacked_bar"
               ? (widget.config as StackedBarConfig).stacked !== false   // default ON
               : Boolean((widget.config as AreaConfig).stacked)          // default OFF
           }
           onChange={(e) => setStacked(e.target.checked)}
           aria-label="Stack series"   // <-- aria-label is "Stack series" for BOTH types
         />
         <span>Stack multiple series</span>
       </label>
     </Section>
   )}
   ```
   Note the asymmetry: `stacked_bar` label is **"Stack mode"** and default is
   `stacked !== false` (checked unless explicitly false); `area` label is
   **"Stack series"** and default is `Boolean(stacked)` (unchecked unless set).
   The `aria-label` is `"Stack series"` for both. Keep all three facts.

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
  outsidePress: true,             // close on outside pointerdown
  escapeKey: true,                // close on Escape
});
const role = useRole(context, { role: "dialog" });
const { getFloatingProps } = useInteractions([dismiss, role]);
```

Render with portal + focus manager:

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
  reference element is the *selected widget shell*, resolved by the page. When
  `anchorEl` is `null` the page does not render the popover (it's gated on
  `anchorEl` truthiness), but guard inside as well.
- **`autoUpdate`** (via `whileElementsMounted`) keeps the popover glued to the
  widget as the user scrolls the canvas, resizes the window, or drags the
  widget.
- **No canvas reflow:** the popover is in a `FloatingPortal` (renders at the
  document body, positioned via `floatingStyles`), so it is never a flex
  sibling of the canvas. This is the structural fix.
- **querySelector anchor staleness guard:** because `anchorEl` is resolved by a
  `document.querySelector` lookup (not a live ref), the node can go stale —
  e.g. the widget is removed, or the canvas re-renders the shell. If the
  resolved node detaches, the popover would float against a dead element. Guard
  it: when `anchorEl` becomes disconnected, close (deselect):
  ```tsx
  useEffect(() => {
    if (anchorEl && !anchorEl.isConnected) onClose();
  }, [anchorEl, onClose]);
  ```
  Note the timing: `anchorEl` is set by the page in a post-commit `useEffect`
  keyed on the selected id, so on the render that re-selects a widget the
  popover mounts on the **second** render (after the effect runs and sets
  state). Tests must account for this (see Test timing below).

### Test timing — popover mounts on a SECOND render

The page computes `anchorEl` in a `useEffect` keyed on the selected-widget id,
so selecting a widget does NOT mount the popover synchronously — it mounts on
the next render after the effect resolves the node and sets state. **Every
popover-presence assertion at the page level MUST be async:**

```tsx
await waitFor(() => screen.getByTestId("widget-editor-popover"));
```

Never assert the popover synchronously with `getByTestId` immediately after the
selecting click — it will not be in the DOM yet on the first render.

### jsdom test handling for floating-ui

floating-ui calls `getBoundingClientRect` / `ResizeObserver` /
`requestAnimationFrame`; jsdom returns all-zero rects and lacks
`ResizeObserver`. Important facts:

- **`ResizeObserver` polyfill goes in `vitest.setup.ts` (GLOBAL, not
  per-file).** jsdom 26 has no `ResizeObserver`; floating-ui's `autoUpdate`
  needs it. Add it once in setup (Task 1b) so every test that ever portals a
  floating element is covered, not just this file.
- **floating-ui renders and positions fine in jsdom.** All-zero rects collapse
  to `translate(0, 0)`, so the popover element really mounts and is queryable —
  these are **real tests, not hollow ones.** Do not skip popover tests as
  "can't test positioning"; assert structure/behavior (the popover renders, the
  right tab panel shows, Escape closes, `role`/`aria` present, `aria-selected`
  toggles).
- **Dismiss assertions:** prefer **Escape** as the primary, unambiguous close
  test (`fireEvent.keyDown(document, { key: "Escape" })` →`onClose`). If you
  also test outside-press, use **`fireEvent.pointerDown(document.body)`** —
  floating-ui's `useDismiss` listens on **pointerdown**, NOT mousedown; a
  `mouseDown` will not trigger dismissal and the test will falsely fail.
- **`FloatingPortal` renders into `document.body`;** query with `screen.*`
  (whole-document search), not `within(container)`.

---

## TDD ground rules (both PRs)

Commands (run inside the frontend container; for parallel-agent sessions use an
isolated compose project per CLAUDE.md — `docker compose -p team-<name> …` on
**every** call):

- Single file: `docker compose exec -T frontend npx vitest run <path>`
- Full suite: `docker compose exec -T frontend npx vitest run`
- Type-check: `docker compose exec -T frontend npx tsc --noEmit`
- Lint (CI gate): `docker compose exec -T frontend npx eslint . --quiet`

Commit after each task (no AI attribution in messages, no Co-Authored-By, per
user rule). PR titles are conventional-commit (squash-merge → PR title is the
release subject).

**Full-suite rule:** never trust single-file runs; a cross-file rename can pass
the touched file and break another (see
`reference_frontend_full_suite_verification.md`). Each PR's final gate runs the
**entire** vitest suite + `tsc` + `eslint`.

---

# PR 1 — Extraction refactor (ZERO behavior change)

**Theme:** add the dependency, extract every ConfigRail sub-block into
reusable components, build + unit-test `WidgetEditorPopover` — but **leave
`ConfigRail.tsx` rendering the extracted pieces** so both pages and all
existing tests stay green. The popover is built and unit-tested here but is
**NOT yet wired into either page.** Re-home the 3 component test files onto the
extracted components in this PR.

PR title: `refactor(reports): extract ConfigRail into reusable widget-editor pieces`

---

### Task 1a — Add the `@floating-ui/react` dependency

**Files:** `package.json`, `package-lock.json`

No test (dependency-only). Verification is install + import resolves.

1. Add `"@floating-ui/react": "^0.27.0"` (or current latest) to `dependencies`
   in `package.json`.
2. Regenerate the lockfile **inside the container** so the platform-correct
   tree lands: `docker compose exec -T frontend npm install` (updates
   `package-lock.json`; commit both files).
3. **Rebuild note:** the dev image COPYs `package.json`/lockfile but a running
   container won't have the new dep until rebuilt:
   `docker compose up --build -d frontend`. A stale image fails `import`
   resolution locally while CI (fresh build) is green (dev-unmounted/stale-image
   gotcha — `reference_dockerfile_dev_install.md`).
4. Verify: `docker compose exec -T frontend node -e "require.resolve('@floating-ui/react')"`.

**Commit:** `build(reports): add @floating-ui/react for widget editor popover`

---

### Task 1b — Global `ResizeObserver` polyfill in vitest setup

**Files:** `frontend/vitest.setup.ts`

`vitest.setup.ts` currently has no `ResizeObserver`. floating-ui's `autoUpdate`
needs it. Add it **once, globally** (not per test file).

**Implement:** append to `vitest.setup.ts`:
```ts
// floating-ui's autoUpdate calls ResizeObserver; jsdom 26 has none.
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}
```

**Run-to-pass:** `docker compose exec -T frontend npx vitest run` (suite still
green; this is additive). `npx tsc --noEmit`.

**Commit:** `test(reports): add global ResizeObserver polyfill for floating-ui`

---

### Task 2 — Extract control constants + helpers

**Files:** `components/reports/config/controlConstants.ts` (new)

**Failing test:** optional `tests/components/reports/config/controlConstants.test.ts`
asserting `DIMENSION_OPTIONS.length === 9` and `AGG_OPTIONS.length === 4` to
lock the move. (Otherwise covered transitively by Task 3/4 tests.)

**Implement:** copy **verbatim** from `ConfigRail.tsx`: `AGG_OPTIONS` (61-66),
`AGG_HELP_KEY` (69-74), `FIELD_OPTIONS` (78-80, built from
`MEASURE_FIELD_LABELS` in `lib/reports/series.ts`), `DIMENSION_OPTIONS`
(82-92), `MAX_SERIES`/`MAX_TABLE_COLUMNS` (94-95), `isMultiSeries` (98-107),
`isSingleAggLocked` (110-112). Export each; keep `HelpTooltipKey` typing.
Also extract the small `Section` presentational component (ConfigRail 399-418)
here (or a shared `Section.tsx`) since every extracted piece uses it.

**Run-to-pass:** `docker compose exec -T frontend npx tsc --noEmit`

**Commit:** `refactor(reports): extract widget-config control constants`

---

### Task 3 — Extract measure editors (single + multi-series)

**Files:** `components/reports/config/SingleMeasureEditor.tsx`,
`components/reports/config/MeasuresEditor.tsx` (both new),
`tests/components/reports/config/measure-editors.test.tsx` (new)

**Failing test first** (`measure-editors.test.tsx`):
- `SingleMeasureEditor`: rendering `{agg:"sum",field:"amount"}`, changing
  **Aggregation** to `count` calls `onChange` with `{agg:"count",field:"amount"}`;
  changing **Field** to `id` calls with `{agg:"sum",field:"id"}`. Query by the
  existing `aria-label="Aggregation"` / `"Field"`.
- `MeasuresEditor`: with a `line` widget (2 series), `data-testid="measure-add"`
  appends `{measure:{agg:"sum",field:"amount"}}`; `measure-remove-1` removes
  index 1; remove is hidden when only one series; cap respected at `MAX_SERIES`;
  a `table` widget caps at `MAX_TABLE_COLUMNS` and labels rows "Column N".

Run-to-fail: `docker compose exec -T frontend npx vitest run tests/components/reports/config/measure-editors.test.tsx`

**Implement:** move `SingleMeasureEditor` (ConfigRail 431-474) and
`MeasuresEditor` (476-598) verbatim into their own files, importing from
`controlConstants.ts` and the shared `Section`/`HelpTooltip`. Keep ALL
`data-testid`s (`measure-row-N`, `measure-add`, `measure-remove-N`) and
`aria-label`s identical — downstream tests and parity depend on them.

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
  Date range label (drives through `isFieldOverridden` from `resolve.ts` — do
  NOT reimplement it).
- Changing amount min calls `onChange` with the merged `amount_range`.

> Mock `@/lib/api`'s `apiFetch` the way the existing
> `override-pill-pickers.test.tsx` does (route `/categories`, `/accounts`,
> `/tags` to fixtures) so `AccountFilter`/`CategoryPicker`/`TagFilter` don't
> hang on mount. Mirror that file's `mockApi()` helper.

Run-to-fail: `docker compose exec -T frontend npx vitest run tests/components/reports/config/FilterEditor.test.tsx`

**Implement:** move `FilterEditor` (600-731), `TxnTypeRadioRow` (733-766),
`OverridePill` (420-429) verbatim. Keep `isFieldOverridden` import from
`@/lib/reports/resolve`. No logic change.

Run-to-pass + `tsc --noEmit`.

**Commit:** `refactor(reports): extract per-widget FilterEditor`

---

### Task 5 — Extract the mutation closures into `useWidgetMutations`

**Files:** `components/reports/config/useWidgetMutations.ts` (new)

The `setTitle / setFilters / setSingleMeasure / setSeries / setPrimaryDimension
/ setSecondaryDimension / setComparePrior / setTopN / setStacked` closures are
currently local to `ConfigRail` (lines 122-226). Port them **verbatim** into a
shared hook `useWidgetMutations(widget, onUpdate)` that returns the same named
functions. One home, identical logic, consumed by `DataTab`/`StyleTab`/popover
and (in PR1) by `ConfigRail` itself.

**Failing test:** optional focused unit test asserting each setter produces the
same `onUpdate` payload it does today (e.g. `setSecondaryDimension("")` splices
`dimensions[1]`). Otherwise covered by Tasks 3/6 + the unchanged ConfigRail
tests.

**Implement:** copy each closure body **character-for-character** (the casts and
the `isMultiSeries`/`isSingleAggLocked` guards inside them are load-bearing —
e.g. `setSingleMeasure` early-returns on `isMultiSeries`, `setSecondaryDimension`
early-returns on `kpi`/`isSingleAggLocked`). Import the helpers from
`controlConstants.ts`.

Run-to-pass: `tsc --noEmit`.

**Commit:** `refactor(reports): extract widget mutation closures into a hook`

---

### Task 6 — Data + Style tab composers

**Files:** `components/reports/config/DataTab.tsx`,
`components/reports/config/StyleTab.tsx` (new). Focused tab unit tests
encouraged; also covered by the popover test (Task 7).

**Implement `DataTab`** — consumes `widget` + `useWidgetMutations`:
- Data source: disabled `Transactions` select (verbatim, ConfigRail 256-265).
- Measures: the **verbatim measure branch** named above (ConfigRail ~270-283) —
  `isMultiSeries(widget) ? <MeasuresEditor onChange={setSeries}/> :
  <SingleMeasureEditor measure={(widget.config as KPIConfig|BarConfig|PieConfig|SparklineConfig).measure} onChange={setSingleMeasure}/>`.
- Primary dimension select (hidden for `kpi`), `setPrimaryDimension`
  (ConfigRail 285-302).
- Secondary dimension select (only `bar`/`table`), `setSecondaryDimension`
  (ConfigRail 304-340 — keep the `bar`→"Break down by (optional)" /
  `table`→"Secondary dimension (optional)" label split and the matching
  `aria-label`s "Break down by" / "Secondary dimension").

**Implement `StyleTab`** — consumes `widget` + `useWidgetMutations`:
- Title input, `setTitle` (ConfigRail 246-254).
- KPI: compare_prior_period checkbox, `setComparePrior` (342-356).
- Pie: top_n number, `setTopN` (358-370).
- Area/StackedBar: the **verbatim stacked branch** named above (372-388) — keep
  the "Stack mode"/"Stack series" label split, the `stacked !== false` vs
  `Boolean(stacked)` default split, and the shared `aria-label="Stack series"`.
- (NOT `Line.smooth` / `format` — see deviation note in Self-Review.)

`tsc --noEmit` must pass.

**Commit:** `feat(reports): add Data and Style tab composers for widget editor`

---

### Task 7 — WidgetEditorPopover shell (floating-ui + tabs + a11y)

**Files:** `components/reports/WidgetEditorPopover.tsx` (new),
`tests/components/reports/WidgetEditorPopover.test.tsx` (new)

> `ResizeObserver` is already global from Task 1b — no per-file polyfill.

**Failing test first** (`WidgetEditorPopover.test.tsx`). Render with a non-null
`anchorEl` (`const el = document.createElement("div"); document.body.appendChild(el)`):

1. Renders `data-testid="widget-editor-popover"` with `role="dialog"` and
   `aria-label="Widget settings"`.
2. **Default tab = Data:** the Data panel (data-source select + measure
   controls) is visible; Filters/Style panels are not.
3. **Tablist a11y:** three `role="tab"` buttons; active tab has
   `aria-selected="true"`; tabpanels labelled via `aria-labelledby`. Clicking
   "Filters" shows the FilterEditor; clicking "Style" shows the title input.
4. **Per-type visibility:**
   - `kpi`: Data shows measure + source but **no** primary/secondary dimension;
     Style shows compare checkbox.
   - `pie`/`sparkline`: single measure + primary dimension, **no** secondary;
     Style shows top_n only for pie.
   - `bar`: primary + secondary ("Break down by"); no Style type-knob.
   - `line`/`area`/`stacked_bar`/`table`: `MeasuresEditor` (add/remove);
     `table`+`bar` show secondary dimension; area/stacked_bar show the stacked
     checkbox in Style.
5. **Dismiss:** `fireEvent.keyDown(document, { key: "Escape" })` calls
   `onClose`; `fireEvent.pointerDown(document.body)` (NOT mouseDown) calls
   `onClose`.

> Component-level mount here is synchronous (you pass `anchorEl` directly), so
> these in-component assertions need NOT `waitFor`. The second-render timing
> only applies to the page-level tests in PR2 (where the page computes
> `anchorEl` in an effect).

Run-to-fail: `docker compose exec -T frontend npx vitest run tests/components/reports/WidgetEditorPopover.test.tsx`

**Implement:** the floating-ui shell exactly as in "Floating-UI wiring",
including the `!anchorEl.isConnected` staleness guard effect. Header:
`<h2>Widget settings</h2>` + a `Close` button (`onClick={onClose}`) mirroring
ConfigRail's header (230-244). Tablist:

```tsx
const [tab, setTab] = useState<"data" | "filters" | "style">("data");
// role="tablist" with three role="tab" buttons; wire aria-selected +
// aria-controls + id/aria-labelledby on each tab/panel pair. Arrow-key roving
// is optional for 4a (Tab/click is enough).
```

Tab panels: Data → `<DataTab>`, Filters →
`<FilterEditor filters={widget.config.filters ?? {}} canvasFilters onChange={setFilters}/>`,
Style → `<StyleTab>`. Per-type sub-control visibility lives inside
`DataTab`/`StyleTab` (they branch on `widget.type`); the popover always renders
all three tabs (every tab is meaningful for every type).

Run-to-pass + `tsc --noEmit`.

**Commit:** `feat(reports): widget editor popover shell with tabs and a11y`

---

### Task 8 — Re-point ConfigRail at the extracted pieces (keep it rendering)

**Files:** `components/reports/ConfigRail.tsx` (modified, NOT deleted in PR1)

**Goal:** `ConfigRail.tsx` now *imports and renders* the extracted pieces
(`Section`, `SingleMeasureEditor`, `MeasuresEditor`, `FilterEditor`,
`controlConstants`, `useWidgetMutations`) instead of defining them inline. Its
external behavior, markup, `data-testid="config-rail"`, every `aria-label`, and
every `onUpdate` payload stay **byte-identical** so both pages and ALL existing
ConfigRail tests stay green untouched.

**No new failing test** — the regression bar is the **existing** ConfigRail
suite passing unchanged:
- `tests/components/reports/config-rail-tooltips.test.tsx`
- `tests/components/reports/override-pill-pickers.test.tsx`
- `tests/components/reports/config-rail-secondary-dimension.test.tsx`
- the `config-rail` assertions in `tests/app/reports-editor-page.test.tsx`

**Implement:** replace ConfigRail's inline definitions with imports of the
extracted modules; have ConfigRail compose `useWidgetMutations` + the tab pieces
(or simply render the same Sections via the extracted components). Keep the
`<aside data-testid="config-rail" className="… w-80 shrink-0 …">` wrapper.

Run-to-pass (all four existing suites):
`docker compose exec -T frontend npx vitest run tests/components/reports/config-rail-tooltips.test.tsx tests/components/reports/override-pill-pickers.test.tsx tests/components/reports/config-rail-secondary-dimension.test.tsx tests/app/reports-editor-page.test.tsx`
then `tsc --noEmit`.

**Commit:** `refactor(reports): ConfigRail renders the extracted widget-editor pieces`

---

### Task 9 — Re-home the three orphaned component test files

**Files:**
`tests/components/reports/config-rail-tooltips.test.tsx`,
`tests/components/reports/override-pill-pickers.test.tsx`,
`tests/components/reports/config-rail-secondary-dimension.test.tsx`

These three files import `@/components/reports/ConfigRail` directly and render
it 4× / 5× / 5× respectively (~538 lines of coverage total). They will fail to
compile the moment `ConfigRail` is deleted in PR2. **Re-home each onto the
extracted component that now owns the behavior under test** — do NOT silently
drop them. Re-point them in **PR1** (while both ConfigRail and the extracted
pieces exist) so the coverage migrates cleanly and PR2 only deletes the
component.

- `config-rail-tooltips.test.tsx` → re-point at the **extracted Sections that
  bear the `HelpTooltip`** (the aggregation explainer lives in
  `SingleMeasureEditor`/`MeasuresEditor`; the master-category explainer lives on
  the primary/secondary dimension Sections in `DataTab`). Render `DataTab`
  (covers both the agg tooltip and the dimension master-category tooltips) and/or
  the measure editors directly. Keep the same `HELP_TOOLTIPS[...].triggerLabel`
  assertions.
- `override-pill-pickers.test.tsx` → re-point at the extracted **`FilterEditor`**
  (it owns the override pill + the picker filters). Swap
  `<ConfigRail widget=… canvasFilters=… onUpdate=… onClose=…/>` for
  `<FilterEditor filters={widget.config.filters ?? {}} canvasFilters=… onChange=…/>`.
  Keep the `override-pill` presence/absence equality assertions.
- `config-rail-secondary-dimension.test.tsx` → re-point at the extracted
  **`DataTab`** (it owns the primary/secondary dimension selects and their
  per-type visibility). Keep the "Break down by" / "Secondary dimension"
  `getByLabelText` visibility matrix and the `dimensions[1]` set/clear assertion.

Rename the files to match their new subjects (e.g.
`tests/components/reports/config/data-tab-tooltips.test.tsx`,
`.../config/filter-editor-override-pill.test.tsx`,
`.../config/data-tab-secondary-dimension.test.tsx`) so no test still references
ConfigRail by name after PR2.

Run-to-pass: run all three migrated files, then `tsc --noEmit`.

**Commit:** `test(reports): re-home ConfigRail component tests onto extracted pieces`

---

### Task 10 — PR1 pre-PR gate (eslint + tsc + FULL vitest)

**Files:** none (verification gate).

1. `docker compose exec -T frontend npx eslint . --quiet` → clean.
2. `docker compose exec -T frontend npx tsc --noEmit` → clean.
3. `docker compose exec -T frontend npx vitest run` → **entire** suite green
   (cross-file rename safety — `reference_frontend_full_suite_verification.md`).
4. Confirm **both pages still render `<ConfigRail>` unchanged** (PR1 wires
   nothing into pages): `git diff --stat` shows no change to
   `app/reports/[id]/page.tsx` or `app/reports/new/page.tsx`.
5. Confirm `Canvas.tsx` and `CanvasFiltersBar.tsx` untouched.

PR1 is openable once 1-4 are green.

---

# PR 2 — Wire-up + delete ConfigRail

**Theme:** mount `WidgetEditorPopover` into **BOTH** pages, remove ConfigRail
from both flex rows, add the `anchorEl` effect + staleness guard, **DELETE
`ConfigRail.tsx`**, migrate the page tests to the popover (`waitFor` + Escape),
and add the reflow-invariance regression test for **both** pages.

PR title: `feat(reports): replace ConfigRail with anchored widget editor popover`

---

### Task 11 — Anchor the popover + WidgetShell a11y

**Files:** `components/reports/WidgetShell.tsx`

**Decision (anchor mechanism):** `WidgetShell` already renders
`data-widget-shell={widgetId}` on its root `div` (line 35). The lowest-risk
anchor is to resolve the selected widget's node by that attribute in each page
(see Tasks 12/13). This avoids threading a ref callback through `Canvas`'s
frozen `renderWidget`→`WidgetShell` plumbing.

**WidgetShell change (a11y):** add `aria-haspopup="dialog"` and
`aria-expanded={selected}` to the selectable root `div` so the selected widget
announces it controls a dialog. Keep `onClick={onSelect}` and all existing
attributes/classes.

No standalone test (covered by Tasks 12/13). `tsc --noEmit` must pass.

**Commit:** `feat(reports): widget shell announces it anchors the editor dialog`

---

### Task 12 — Wire popover into `[id]/page.tsx` + migrate its tests + reflow test

**Files:** `app/reports/[id]/page.tsx`,
`tests/app/reports-editor-page.test.tsx`

**Failing tests first** (update + add in `reports-editor-page.test.tsx`). Note
the **second-render timing**: every popover-presence assertion is `await
waitFor(...)`, never synchronous.

a. **Migrate the three `config-rail` references** to `widget-editor-popover`:
   - ~line 237 ("adds a KPI widget…"): after adding a KPI it's selected →
     `await waitFor(() => screen.getByTestId("widget-editor-popover"))` instead
     of `getByTestId("config-rail")`.
   - ~line 391 ("shows the 'Overrides canvas' pill…"): after selecting the
     widget, `await waitFor(() => screen.getByTestId("widget-editor-popover"))`,
     then **switch to the Filters tab** before asserting `override-pill` (the
     pill now lives in the Filters tab, not an always-visible rail):
     `fireEvent.click(screen.getByRole("tab", { name: /filters/i }))`.
   - ~line 902 ("after a successful save lands on the read-only view…"):
     `queryByTestId("config-rail")` → `queryByTestId("widget-editor-popover")`
     must be null in view mode.

b. **NEW reflow-invariance test** — "opening the widget editor popover does not
   reflow the canvas (saved editor)":
   - Load the report-with-widget fixture, enter edit mode. Add
     `data-testid="report-canvas-column"` to the `flex-1` canvas column div
     (page line 752) so it's addressable.
   - Before select: `report-canvas-column` is the **only** element-child of the
     `flex flex-1 overflow-hidden` row (line 751).
   - Click the widget → `await waitFor(() => screen.getByTestId("widget-editor-popover"))`
     (it appears in the portal at `document.body`, NOT inside
     `report-canvas-column`).
   - After select: `report-canvas-column` is **still** the only element-child of
     the flex row (popover portaled to body, never a `w-80` sibling). This is
     the regression guard for the reflow bug.

Run-to-fail: `docker compose exec -T frontend npx vitest run tests/app/reports-editor-page.test.tsx`

**Implement `[id]/page.tsx`:**
1. Replace `import ConfigRail` (line 51) with
   `import WidgetEditorPopover from "@/components/reports/WidgetEditorPopover";`
2. Add `anchorEl` state + effect keyed on `editModeActive` + `selectedWidgetId`
   + `layout.widgets`:
   ```tsx
   const [anchorEl, setAnchorEl] = useState<HTMLElement | null>(null);
   useEffect(() => {
     if (!editModeActive || !selectedWidgetId) { setAnchorEl(null); return; }
     setAnchorEl(
       document.querySelector(`[data-widget-shell="${selectedWidgetId}"]`) as HTMLElement | null,
     );
   }, [editModeActive, selectedWidgetId, layout.widgets]);
   ```
3. In the `flex flex-1 overflow-hidden` row (751): keep ONLY the canvas column
   (752), add `data-testid="report-canvas-column"` to it. **Remove the
   `{editModeActive && selectedWidget && (<ConfigRail …/>)}` block (822-829).**
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

Run-to-pass on the page test, then `tsc --noEmit`.

**Commit:** `feat(reports): mount widget editor popover in saved report editor`

---

### Task 13 — Wire popover into `new/page.tsx` + reflow test

**Files:** `app/reports/new/page.tsx`, plus a draft-page test
(extend an existing draft test file, or add
`tests/app/reports-draft-popover.test.tsx`)

> **The draft page gates differently:** `new/page.tsx` is **always** in edit
> mode — there is NO `editModeActive` flag. Today it renders ConfigRail on
> `selectedWidget` alone (lines 310-317). The popover gate is
> `selectedWidget && anchorEl` (no `editModeActive`). The `anchorEl` effect
> likewise omits `editModeActive`.

**Failing test first** (draft page):
- After adding/selecting a widget,
  `await waitFor(() => screen.getByTestId("widget-editor-popover"))`.
- **Reflow-invariance (draft):** the `flex flex-1 overflow-hidden` row (line
  266) has exactly one element-child (`report-canvas-column`, added to the
  `flex-1` div at line 267) both before and after a widget is selected.

Run-to-fail on that test path.

**Implement `new/page.tsx`:**
1. Replace `import ConfigRail` (line 34) with the `WidgetEditorPopover` import.
2. Add `anchorEl` state + effect keyed on `selectedWidgetId` + `layout?.widgets`
   (NO `editModeActive`):
   ```tsx
   const [anchorEl, setAnchorEl] = useState<HTMLElement | null>(null);
   useEffect(() => {
     if (!selectedWidgetId) { setAnchorEl(null); return; }
     setAnchorEl(
       document.querySelector(`[data-widget-shell="${selectedWidgetId}"]`) as HTMLElement | null,
     );
   }, [selectedWidgetId, layout?.widgets]);
   ```
3. In the flex row (266): keep ONLY the canvas column (267), add
   `data-testid="report-canvas-column"`. **Remove the
   `{selectedWidget && (<ConfigRail …/>)}` block (310-317).**
4. After the flex row, mount the popover gated on
   `selectedWidget && anchorEl` (NOT `editModeActive`):
   ```tsx
   {selectedWidget && anchorEl && (
     <WidgetEditorPopover
       widget={selectedWidget}
       canvasFilters={canvasFilters}
       anchorEl={anchorEl}
       onUpdate={updateWidget}
       onClose={() => setSelectedWidgetId(null)}
     />
   )}
   ```

Run-to-pass + `tsc --noEmit`.

**Commit:** `feat(reports): mount widget editor popover in draft report editor`

---

### Task 14 — Delete ConfigRail

**Files:** delete `components/reports/ConfigRail.tsx`

By now nothing imports it: both pages use the popover (Tasks 12/13) and all
three component tests were re-homed in PR1 (Task 9). Delete the file.

Run-to-pass: `tsc --noEmit` (no dangling import).

**Commit:** `refactor(reports): delete ConfigRail`

---

### Task 15 — PR2 pre-PR gate (eslint + tsc + FULL vitest + grep gate)

**Files:** none (verification gate).

1. `docker compose exec -T frontend npx eslint . --quiet` → clean.
2. `docker compose exec -T frontend npx tsc --noEmit` → clean.
3. `docker compose exec -T frontend npx vitest run` → **entire** suite green.
4. **Grep gate (must be zero across BOTH pages and all tests):**
   `grep -rn "config-rail\|ConfigRail" frontend/` → **zero hits**. This proves:
   the component is deleted, `[id]/page.tsx` AND `new/page.tsx` both dropped
   their imports, and no orphaned test import survives.
5. Confirm `Canvas.tsx`, `CanvasFiltersBar.tsx`, and `lib/reports/resolve.ts`
   are untouched in the diff (`git diff --stat`).

PR2 is openable once 1-5 are green.

---

## Must-flag list (implementer: handle each explicitly)

1. **Second ConfigRail consumer — `new/page.tsx`.** Both `[id]/page.tsx` AND
   `new/page.tsx` mount ConfigRail in an identical `flex flex-1 overflow-hidden`
   row and BOTH must migrate. Deleting ConfigRail without wiring `new/page.tsx`
   breaks the build. **Gate difference:** the draft page is always in edit mode
   — its popover gate is `selectedWidget && anchorEl` (no `editModeActive`), and
   its `anchorEl` effect omits `editModeActive`. (PR2 Tasks 12 vs 13.)
2. **Three orphaned component test files re-homed, not dropped** (~538 lines):
   `config-rail-tooltips` → DataTab/measure editors; `override-pill-pickers` →
   `FilterEditor`; `config-rail-secondary-dimension` → `DataTab`. Re-pointed in
   **PR1 Task 9** while both old and new components exist; renamed off the
   `config-rail` name so the PR2 grep gate passes.
3. **Second-render test timing.** `anchorEl` is computed in a `useEffect` keyed
   on the selected id, so the popover mounts on the **second** render after
   selection. ALL page-level popover-presence assertions use
   `await waitFor(() => screen.getByTestId("widget-editor-popover"))`, never
   synchronous `getByTestId`. (Component-level tests in PR1 Task 7 pass
   `anchorEl` directly and are synchronous.)
4. **querySelector anchor staleness guard.** The popover `onClose`s when the
   resolved node detaches: `useEffect(() => { if (anchorEl && !anchorEl.isConnected) onClose(); }, [anchorEl, onClose]);`.
   Remember the post-commit effect timing (state is set after render).
5. **floating-ui in jsdom:** `ResizeObserver` polyfill is GLOBAL in
   `vitest.setup.ts` (PR1 Task 1b), not per-file. Prefer **Escape** as the
   primary close assertion; for outside-press use `fireEvent.pointerDown`
   (floating-ui `useDismiss` listens on **pointerdown**, NOT mousedown). jsdom
   renders+positions floating-ui fine (all-zero rects → `translate(0,0)`), so
   popover tests are real, not hollow.
6. **Verbatim transcription hotspots.** Two branches must move
   character-for-character: (a) the measure branch with the
   `(widget.config as KPIConfig|BarConfig|PieConfig|SparklineConfig).measure`
   cast → `DataTab`; (b) the `area`/`stacked_bar` stacked branch → `StyleTab`,
   keeping BOTH the label split ("Stack mode" vs "Stack series") AND the default
   split (`stacked_bar`: `stacked !== false`; `area`: `Boolean(stacked)`) AND
   the shared `aria-label="Stack series"`.
7. **Reflow invariance is the core regression guard** — assert the canvas
   column (`report-canvas-column`) stays the **sole element-child** of the flex
   row when the popover opens, on **both** pages (PR2 Tasks 12b + 13). The
   popover is portaled to `document.body`, never a `w-80` flex sibling.
8. **`No Off-Token` design rule:** every color from theme tokens
   (`border-border`, `bg-surface`, `text-text-*`, `ring-accent`, …) — reuse
   ConfigRail's exact classes. `frontend/scripts/check-design-tokens.sh`
   CI-blocks raw Tailwind palette colors.
9. **Frozen surfaces:** do NOT edit `Canvas.tsx`, `CanvasFiltersBar.tsx`,
   `lib/reports/resolve.ts`, or the filter model. Confirm via `git diff --stat`
   in both gates.
10. **Dep + lockfile via the container** (PR1 Task 1a): `npm install` inside the
    frontend container, then `up --build -d frontend`, or a stale dev image
    fails the import locally while CI is green.

---

## Self-Review

**PR split coverage:**
- PR1 = extraction with **zero behavior change**; `ConfigRail.tsx` keeps
  rendering the extracted pieces (Task 8) so both pages + all existing tests
  stay green; popover built + unit-tested (Task 7) but NOT wired; 3 component
  tests re-homed (Task 9). ✔
- PR2 = wire both pages (Tasks 12, 13), `anchorEl` effect + staleness guard,
  delete ConfigRail (Task 14), migrate page tests (waitFor + Escape + Filters
  tab), reflow test for BOTH pages, grep gate = zero `ConfigRail` hits anywhere
  (Task 15). ✔

**Architect's 6 numbered items — where each lands:**
1. Second consumer `new/page.tsx` migrated identically → Goal, PR2 Task 13,
   Must-flag #1. ✔
2. Three orphaned test files re-homed (not dropped) → PR1 Task 9, Must-flag #2. ✔
3. Second-render `waitFor` timing → "Test timing" section, PR2 Tasks 12a/12b/13,
   Must-flag #3. ✔
4. querySelector staleness guard (`!anchorEl.isConnected` → onClose) →
   "Floating-UI wiring" + PR1 Task 7 + Must-flag #4. ✔
5. floating-ui in jsdom: global `ResizeObserver` in `vitest.setup.ts` (Task 1b),
   Escape primary, `pointerDown` (not mouseDown), real-not-hollow → "jsdom test
   handling" + Must-flag #5. ✔
6. Verbatim branches named (measure cast → DataTab; stacked label+default split
   → StyleTab) → "Verbatim branches" section + PR1 Task 6 + Must-flag #6. ✔

**CI gates:** both PRs end on a gate task (Task 10 / Task 15) running
`eslint . --quiet` + `tsc --noEmit` + FULL `vitest run`; PR2 adds the grep gate.
Commands use `docker compose exec -T frontend …` throughout. ✔

**Known deviation (unchanged from prior plan):** ConfigRail renders **no**
`Line.smooth` and **no** `format` control today; the original brief listed both
as Style knobs. To honor "re-housing, not logic rewrite" + the no-new-knobs
non-goal, 4a ports only existing controls and defers `smooth`/`format` to a
fast-follow.

**Type consistency:** all referenced types (`Widget`, `CanvasFilters`,
`WidgetFilters`, `Measure`, `SeriesConfig`, `Dimension`, `KPIConfig`/`BarConfig`/
`PieConfig`/`SparklineConfig`/`LineConfig`/`AreaConfig`/`StackedBarConfig`/
`TableConfig`) exist in `lib/reports/types.ts`. `anchorEl: HTMLElement | null`
matches floating-ui's external-reference contract. `tsc --noEmit` is a gate in
every task.
