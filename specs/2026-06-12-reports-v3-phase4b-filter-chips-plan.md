# Reports v3 Phase 4b — shared-date filter model + per-widget filter chips

**Status:** plan (not implemented)
**Date:** 2026-06-12
**Companion:** visual companion (user-approved). The design below is **locked** — do not relitigate.
**Revision:** absorbs senior-architect race-condition fixes to the chip→popover deep-link (the `requestedTab` consume-and-clear handshake, mobile-stack chip decision, `pickDateRange` export, view-mode orphan-selection guard, draft-page empty-state copy).

---

## Goal

Make every report widget self-describing about what it queries:

1. **Shrink the canvas filter bar to a shared DATE control only.** No accounts/categories at the canvas level. Widgets inherit the shared date and may override it per-widget.
2. **Make accounts and categories fully per-widget** — edited only in the widget popover's Filters tab. The canvas no longer carries `account_ids` / `category_ids`.
3. **Give each widget a slim filter-chip header** (pill chips) showing its **effective non-default filters**, visible in BOTH view and edit mode. Clicking a chip **selects the widget and opens the popover on the Filters tab**.

The chips must mirror exactly what `resolveFilters(canvasFilters, widgetFilters)` produces, so they never lie about what the widget queries.

## Architecture

- **Filter model change (`lib/reports/types.ts`):** `CanvasFilters` keeps `date_range` and **drops** `account_ids` / `category_ids`. `WidgetFilters` is unchanged (keeps `date_range`, `account_ids`, `category_ids`, `txn_type`, `amount_range`, `tag_names`, `tag_match`).
- **Resolution (`lib/reports/resolve.ts`):** `resolveFilters` stops cascading accounts/categories from canvas (widget-only now). `isFieldOverridden` keeps working for `date_range` (the only field still shared); the `account_ids`/`category_ids` branches become dead (canvas no longer has those keys) and are removed/no-op cleanly. **`pickDateRange` is now exported** (it was module-private at ~line 192) so the describe helper can reuse the single date inherit/override implementation rather than reimplementing it.
- **New describe helper (`lib/reports/describe-filters.ts`):** `describeWidgetFilters(widget, canvasFilters, { accounts, categories })` returns an ordered list of **chip descriptors** computed consistently with `resolveFilters`. Pure, unit-tested. Takes already-fetched `accounts: Account[]` and `categories: Category[]` so it can map ids → names; falls back to count labels when a name is unresolved. **Imports `pickDateRange` and `isFieldOverridden` from `resolve.ts`** — single source of truth for the date inherit/override decision.
- **Chip header (`WidgetShell.tsx`):** new slim header row above `children`, rendered in both view and edit mode. It calls `describeWidgetFilters` and renders pill chips. Each chip is a button: `onClick` → `onSelectFilters()` (a new callback that selects the widget AND requests the Filters tab). Account/category names come from SWR-fetched `accounts`/`categories` (same endpoints `AccountFilter`/`CategoryPicker` already use), fetched once at the page level and threaded down (NOT per-widget, to avoid N fetches).
- **Popover deep-link (`WidgetEditorPopover.tsx`) — consume-and-clear handshake:** new `requestedTab?: TabKey` AND `onTabConsumed?: () => void` props, with **two separate effects** (NOT one combined effect — see Task 8 for why the single-effect approach is broken):
  - a **reset effect** keyed on `[widget.id]` only, which resets the tab on widget-identity change;
  - a **honor-request effect** keyed on `[requestedTab]` which, when `requestedTab` is truthy, sets the tab to it and immediately calls `onTabConsumed()` so the page clears the request in the same cycle.
  The page holds `requestedTab` and clears it the instant the popover consumes it. Because of consume-and-clear, **every** chip click is a genuine `null → "filters"` transition — so clicking a chip on the already-selected widget a second time still re-opens Filters.
- **Page wiring (both pages):** add `requestedTab` state set alongside `selectedWidgetId` when a chip fires; threaded into `WidgetEditorPopover` together with `onTabConsumed={() => setRequestedTab(null)}`. `closePopover` ALSO clears `requestedTab` (harmless belt-and-suspenders; the real clear is the consume callback).
- **Backend:** no change. The query endpoint already ignores canvas `account_ids`/`category_ids`; the frontend simply stops sending them. Note only.

### Architect-verified facts (encode, do not relitigate)

- **`Canvas.tsx` is NOT touched.** The new `WidgetShell` props (`widget`, `canvasFilters`, `accounts`, `categories`, `onSelectFilters`) are all supplied by the page-level `renderWidget` closure passed into `Canvas`. `Canvas` only forwards `renderWidget` and never reads filter state, so nothing in `Canvas.tsx` changes.
- **The `CanvasFilters` type shrink surfaces no hidden reads.** Saved `canvas_filters_json` that still carries `account_ids`/`category_ids` is tolerated via the existing `as CanvasFilters` cast at hydrate time — the extra keys are simply not in the type and are never read. **No migration.**
- **`pickList` can be deleted.** Grep confirms it is used only by the two removed account/category cascade branches in `resolveFilters`. Once those switch to reading the widget value directly, `pickList` has no callers and is removed. (`pickDateRange` is a separate helper and STAYS — now exported.)

### Name data source decision: **NAMES, not counts** (with count fallback) — **LOCKED, keep**

`Account` and `Category` both carry a `name` field, and `AccountFilter` (`GET /api/v1/accounts`, SWR key `/api/v1/accounts?for=reports-filter`) and `CategoryPicker` (`GET /api/v1/categories`, SWR key `/api/v1/categories?for=reports-filter`) already fetch the full lists. The chip header reuses those exact same SWR keys (so the cache is shared — no extra network cost) to resolve ids → names. Chips show truncated name lists, e.g. `Groceries +2`. When a list is fetched but an id is unresolved (deleted/inactive account), that id is dropped from the label and the count reflects only resolved names; if NONE resolve (data still loading or all unresolved), the chip falls back to a count label (`2 accounts`). This keeps chips human-readable without ever blocking on load.

## Tech Stack

- Next.js 16 App Router, React 19, TypeScript, Tailwind, SWR.
- Vitest + Testing Library (`renderWithSWR` harness at `tests/utils/render-with-swr`).
- CI gates run inside the frontend container.

## Non-goals

- No backend changes (the AST/query service already tolerates absent canvas account/category).
- No data migration of saved reports. Pre-launch, no backcompat: saved reports whose `canvas_filters_json` still carries `account_ids`/`category_ids` simply have those keys ignored on read (they're no longer in `CanvasFilters`, and `resolveFilters` no longer reads them; the `as CanvasFilters` hydrate cast tolerates the extra keys). Do NOT write a migration script.
- No change to the 8 widget components' internals beyond what `WidgetShell` already wraps (the header lives in the shell, not each widget).
- No keyboard-selection rework for widgets (a known pre-existing gap; out of scope).
- No new chip UI for `status` filters — `WidgetFilters` has no status field today; "status" in the locked decision maps to `txn_type`. (If a future status field lands, the describe helper extends cleanly.)
- **No chips on the mobile read-only stack.** The mobile branch renders `renderWidgetByType` directly, NOT wrapped in `WidgetShell` — so chips never appear there, and that is the intended outcome (mobile is read-only, there is no popover to deep-link into). We do NOT add `WidgetShell` to the stack. See Tasks 9/10.

## File Structure

**New files**
- `frontend/lib/reports/describe-filters.ts` — `describeWidgetFilters()` + `FilterChip` descriptor type + the date/amount label formatters.
- `frontend/tests/lib/reports/describe-filters.test.ts` — unit tests for the helper.
- `frontend/components/reports/WidgetFilterChips.tsx` — presentational chip-row component (rendered by `WidgetShell`); keeps `WidgetShell` lean and independently testable.
- `frontend/tests/components/reports/widget-filter-chips.test.tsx` — render/click tests for the chip row.

**Edited files**
- `frontend/lib/reports/types.ts` — drop `account_ids`/`category_ids` from `CanvasFilters`.
- `frontend/lib/reports/resolve.ts` — widget-only accounts/categories; prune dead canvas branches; delete `pickList`; **export `pickDateRange`**.
- `frontend/components/reports/CanvasFiltersBar.tsx` — date-only.
- `frontend/components/reports/WidgetShell.tsx` — add chip header + `onSelectFilters` + `widget`/`canvasFilters`/`accounts`/`categories` props.
- `frontend/components/reports/WidgetEditorPopover.tsx` — `requestedTab` + `onTabConsumed` props; two-effect consume-and-clear.
- `frontend/app/reports/[id]/page.tsx` — `requestedTab` state, fetch accounts/categories, thread props, view-mode no-op handler, empty-state copy.
- `frontend/app/reports/new/page.tsx` — same wiring (always edit mode), draft empty-state copy.
- `frontend/tests/lib/reports/resolve.test.ts` — rework canvas account/category cases.
- `frontend/tests/components/reports/config/filter-editor-override-pill.test.tsx` — rework: only `date_range` can be a canvas-vs-widget override now; the account/category override-pill cases must change (account/category are widget-only → never an "override").

---

## PR strategy: **ONE PR**

This is smaller than Phase 4a. No new dependency, no large new component (the chip row is a thin presentational component reusing existing SWR keys and the existing pill visual register from `OverridePill`). The change is one coherent model shift — splitting it would leave an intermediate state where the canvas still has account/category inputs but resolve ignores them, or where chips reference a model that hasn't shrunk yet. Ship as a single PR. Title:

```
feat(reports): shared-date canvas filter + per-widget filter chips
```

All CI gates below are **pre-PR** steps.

---

## TDD Tasks (bite-sized)

> Test command: `docker compose exec -T frontend npx vitest run <path>`
> Type-check: `docker compose exec -T frontend npx tsc --noEmit`
> Lint (CI gate): `docker compose exec -T frontend npx eslint . --quiet`

Work in dependency order: model → resolve → describe helper → chip row → shell → popover → pages → rework existing tests → final gates.

---

### Task 1 — Shrink `CanvasFilters` to date-only (type)

**Files:** `frontend/lib/reports/types.ts`

**Failing test:** add to `frontend/tests/lib/reports/resolve.test.ts` a compile-level assertion that canvas account/category no longer cascade (see Task 3); for this task the "failing" signal is `tsc`. Add a tiny type test instead — append to a new `describe-filters.test.ts` stub later. For Task 1 specifically, drive failure via `tsc`:

1. **Run-to-fail (baseline):** `docker compose exec -T frontend npx tsc --noEmit` — currently GREEN (nothing changed yet). This task is a refactor whose failure surfaces in dependents (resolve, CanvasFiltersBar, FilterEditor). To make the failure explicit first, do Task 2's test edit before this — OR accept that Task 1+2+3 land together as the "model" unit. **Chosen approach:** treat Tasks 1–3 as one red→green unit (edit the resolve test red first, see Task 3).

**Implement:**
```ts
export interface CanvasFilters {
  date_range?: CanvasDateRange;
}
```
Remove `account_ids` / `category_ids`.

**Run-to-pass:** deferred to Task 3 (tsc + resolve test green together).

**Commit:** fold into Task 3 commit (`refactor(reports): canvas filters are date-only`).

---

### Task 2 — Rework `resolve.test.ts` canvas account/category cases (RED first)

**Files:** `frontend/tests/lib/reports/resolve.test.ts`

**Failing test:** rewrite the `account_ids` and `category_ids` describe blocks. Under the new model, `isFieldOverridden("account_ids", ...)` and `("category_ids", ...)` must **always return false** (these are widget-only — canvas can't hold them, so a widget value is never an "override"). Replace the five canonical cases per field with assertions that account/category are never overrides, e.g.:

```ts
describe("account_ids (widget-only, never a canvas override)", () => {
  it("returns false even when a widget account list is set", () => {
    expect(isFieldOverridden("account_ids", { account_ids: [1, 2] }, {})).toBe(false);
  });
});
```
Keep all `date_range` cases as-is (date is the only still-shared field). Keep the tag-emission `resolveFilters` block as-is. Add a new `resolveFilters` case: **widget account/category still emit AST filters** (they're widget-only, but still real filters):

```ts
it("emits account_id/category_id filters from the WIDGET (canvas no longer contributes)", () => {
  const out = resolveFilters({ date_range: { start: "2026-01-01", end: "2026-01-31" } },
                             { account_ids: [7], category_ids: [9] });
  expect(out).toContainEqual({ field: "account_id", op: "in", value: [7] });
  expect(out).toContainEqual({ field: "category_id", op: "in", value: [9] });
});
```
Also add a focused export/parity case for `pickDateRange` (now public — Task 3): import it and assert the widget date wins when set, canvas otherwise:
```ts
it("pickDateRange prefers the widget date, falls back to canvas", () => {
  expect(pickDateRange({ start: "2026-02-01" }, { start: "2026-01-01" })).toEqual({ start: "2026-02-01" });
  expect(pickDateRange(undefined, { start: "2026-01-01" })).toEqual({ start: "2026-01-01" });
});
```

**Run-to-fail:** `docker compose exec -T frontend npx vitest run tests/lib/reports/resolve.test.ts` — RED (canvas account/category cases reference old behavior; `pickDateRange` import is not yet exported; `tsc` will object once types shrink).

---

### Task 3 — Make accounts/categories widget-only in `resolve.ts`; export `pickDateRange` (GREEN)

**Files:** `frontend/lib/reports/resolve.ts` (+ Task 1's `types.ts` edit)

**Implement:**
- In `resolveFilters`, replace `pickList(widget?.account_ids, canvas?.account_ids)` with `widget?.account_ids` directly (drop the canvas fallback). Same for `category_ids`.
- **Delete the `pickList` helper.** Grep confirms it is used ONLY by the two account/category cascade branches just removed; nothing else references it. (`pickDateRange` is a separate helper and stays.)
- **Export `pickDateRange`** (currently module-private at ~line 192). Change `function pickDateRange(...)` → `export function pickDateRange(...)`. This is the single source of truth for the date inherit/override decision that Task 5's `describeWidgetFilters` will import (instead of reimplementing the logic).
- In `isFieldOverridden`, the `account_ids`/`category_ids` path now reads `canvasFilters?.[field]` which is always `undefined` → already returns false via the `hasMeaningfulValue(canvasVal)` guard. Tidy: keep the early-return structure; the `valuesEqual` `account_ids`/`category_ids` branch becomes dead — leave it harmless or remove. Document with a one-line comment that only `date_range` is canvas-shared now.

**Run-to-pass:**
- `docker compose exec -T frontend npx vitest run tests/lib/reports/resolve.test.ts` — GREEN
- `docker compose exec -T frontend npx tsc --noEmit` — GREEN (after CanvasFiltersBar/FilterEditor edits in Tasks 4–5; if tsc is red here only because of those two files, proceed to 4–5 then re-run). **Note:** `FilterEditor` calls `isFieldOverridden("account_ids"/"category_ids", ...)` — that still compiles (the field key is valid on `WidgetFilters`), it just always returns false now, so those pills stop rendering. That's intended; the FilterEditor's account/category override pills disappear.

**Commit:** `refactor(reports): canvas filters are date-only; accounts/categories are widget-only`

---

### Task 4 — Canvas bar shrinks to date-only

**Files:** `frontend/components/reports/CanvasFiltersBar.tsx`

**Failing test:** check for an existing CanvasFiltersBar test first (`grep -rl CanvasFiltersBar tests/`). If one exists, update it to assert the account/category pickers are GONE and the date control remains. If none exists, add `frontend/tests/components/reports/canvas-filters-bar.test.tsx`:
```ts
it("renders only the date control (no account/category pickers)", () => {
  render(<CanvasFiltersBar value={{}} onChange={() => {}} />);
  expect(screen.getByTestId("date-preset-chips")).toBeInTheDocument();
  expect(screen.queryByTestId("account-filter")).toBeNull();
  expect(screen.queryByTestId("category-picker")).toBeNull();
});
```

**Run-to-fail:** `docker compose exec -T frontend npx vitest run tests/components/reports/canvas-filters-bar.test.tsx` — RED.

**Implement:** remove the `AccountFilter` and `CategoryPicker` columns and their imports; collapse the grid to a single date column (drop `lg:grid-cols-3`). Keep `data-testid="canvas-filters-bar"` and the date label. Update the component doc comment (it still claims accounts/categories cascade).

**Run-to-pass:** vitest GREEN; `tsc --noEmit` GREEN.

**Commit:** `feat(reports): canvas filter bar is date-only`

---

### Task 5 — `describeWidgetFilters` helper (pure, unit-tested)

**Files:** `frontend/lib/reports/describe-filters.ts` (new), `frontend/tests/lib/reports/describe-filters.test.ts` (new)

**Design the descriptor:**
```ts
export interface FilterChip {
  key: "date" | "txn_type" | "amount" | "tags" | "accounts" | "categories";
  label: string;          // human, truncated, e.g. "Groceries +2"
  overridden?: boolean;   // date only: true when widget overrides the shared canvas date
}
export function describeWidgetFilters(
  widget: Widget,
  canvasFilters: CanvasFilters | undefined,
  lookups: { accounts: Account[]; categories: Category[] },
  now?: Date,
): FilterChip[];
```

**Single source of truth (N2):** import `pickDateRange` and `isFieldOverridden` from `resolve.ts` — do NOT reimplement the date inherit/override logic. The effective date is `pickDateRange(widgetFilters.date_range, canvasFilters?.date_range)`; the `overridden` flag is `isFieldOverridden("date_range", widgetFilters, canvasFilters)`.

**Rules (mirror `resolveFilters`):**
- Read `widget.config.filters` (guard `"filters" in widget.config`).
- **date:** use `pickDateRange(widget, canvas)` (imported). Emit a chip ONLY when an effective date range is set (start or end). Label = preset name if it matches a `buildPresetRanges(now)` entry (reuse the exported `buildPresetRanges` + a match), else `"MMM D – MMM D"` (or `"From MMM D"` / `"Until MMM D"` for one-sided). Set `overridden: true` via `isFieldOverridden("date_range", widgetFilters, canvasFilters)`.
- **accounts:** only when `widgetFilters.account_ids?.length`. Label = resolved names joined, truncated to first 1–2 with `+N` (e.g. `Checking +2`); fall back to `"N accounts"` when zero names resolve.
- **categories:** same pattern against `categories` lookup → `"Groceries +2"` / `"N categories"`.
- **txn_type:** only when set → `"Income"` / `"Expense"` / `"Transfer"` (capitalize).
- **amount:** only when `amount_range` has min or max → `"$100–$500"` / `"≥ $100"` / `"≤ $500"` (plain number formatting; currency symbol optional — keep minimal, match the roadmap deferral on chart currency symbols by NOT inventing per-account currency here; use a bare `$` prefix and note it).
- **tags:** only when `tag_names?.length` → names truncated `Groceries +2`, plus match mode suffix when `any` (e.g. `"tags: a, b (any)"` — keep short). Default `all` shows no suffix.
- Order: date, txn_type, amount, tags, accounts, categories (stable). A widget with no set filters returns `[]`.

**Failing test:** cover each rule:
- empty filters → `[]`.
- date inherited from canvas (no widget date) → ONE date chip, `overridden` falsy, label = preset name when canvas date == a preset.
- widget date overrides canvas → date chip with `overridden: true`.
- accounts resolve to names with `+N` truncation; unresolved id → count fallback.
- txn_type / amount / tags chips appear only when set.
- tag `any` adds the match suffix; `all` does not.

```ts
const w = makeBarWidget({ filters: { account_ids: [1, 2, 3] } });
const chips = describeWidgetFilters(w, {}, { accounts: ACCTS, categories: [] });
expect(chips.find(c => c.key === "accounts")?.label).toBe("Checking +2");
```
Use the optional `now?: Date` arg for date-label determinism (mirrors `DatePresetChips`).

**Run-to-fail:** `docker compose exec -T frontend npx vitest run tests/lib/reports/describe-filters.test.ts` — RED.

**Implement** the helper.

**Run-to-pass:** vitest GREEN; `tsc --noEmit` GREEN.

**Commit:** `feat(reports): describeWidgetFilters chip descriptor helper`

---

### Task 6 — `WidgetFilterChips` presentational row

**Files:** `frontend/components/reports/WidgetFilterChips.tsx` (new), `frontend/tests/components/reports/widget-filter-chips.test.tsx` (new)

**Props:**
```ts
interface Props {
  widget: Widget;
  canvasFilters: CanvasFilters;
  accounts: Account[];
  categories: Category[];
  onSelectFilters: () => void;   // select widget + open Filters tab
}
```
Renders nothing (return `null`) when `describeWidgetFilters` is empty. Otherwise a `flex flex-wrap gap-1` row of pill `<button>`s reusing the `OverridePill` visual register (rounded-full, `bg-accent/15`/`text-accent` for overridden date; `badgeNeutral` from `lib/styles.ts` for the rest). Each button:
- `type="button"`, `data-testid={`widget-filter-chip-${chip.key}`}`,
- `aria-label={`Edit ${chip.key} filter`}`,
- `onClick={(e) => { e.stopPropagation(); onSelectFilters(); }}` (stop propagation so it doesn't double-fire `WidgetShell`'s `onSelect`),
- shows `chip.label`.

**Failing test:**
```ts
it("renders a chip per set filter and fires onSelectFilters on click", async () => {
  const onSelectFilters = vi.fn();
  render(<WidgetFilterChips widget={barWith({ txn_type: "expense" })}
           canvasFilters={{}} accounts={[]} categories={[]}
           onSelectFilters={onSelectFilters} />);
  await userEvent.click(screen.getByTestId("widget-filter-chip-txn_type"));
  expect(onSelectFilters).toHaveBeenCalledOnce();
});
it("renders nothing when the widget has no set filters", () => {
  const { container } = render(<WidgetFilterChips widget={barWith({})} canvasFilters={{}} accounts={[]} categories={[]} onSelectFilters={() => {}} />);
  expect(container).toBeEmptyDOMElement();
});
```

**Run-to-fail:** `docker compose exec -T frontend npx vitest run tests/components/reports/widget-filter-chips.test.tsx` — RED.

**Implement.** **Run-to-pass:** GREEN; `tsc --noEmit` GREEN.

**Commit:** `feat(reports): widget filter-chip row`

---

### Task 7 — Mount chips in `WidgetShell`

**Files:** `frontend/components/reports/WidgetShell.tsx`

**Failing test:** check for an existing WidgetShell test (`grep -rl WidgetShell tests/`); add/extend `frontend/tests/components/reports/widget-shell.test.tsx`:
```ts
it("renders filter chips in both view and edit mode and wires onSelectFilters", async () => {
  const onSelectFilters = vi.fn();
  render(<WidgetShell widgetId="w1" selected={false} editMode={false}
           onSelect={() => {}} onSelectFilters={onSelectFilters}
           widget={barWith({ txn_type: "expense" })}
           canvasFilters={{}} accounts={[]} categories={[]}>
           <div>body</div></WidgetShell>);
  await userEvent.click(screen.getByTestId("widget-filter-chip-txn_type"));
  expect(onSelectFilters).toHaveBeenCalledOnce();
});
```

**Run-to-fail:** RED (props don't exist).

**Implement:** add props `widget: Widget`, `canvasFilters: CanvasFilters`, `accounts: Account[]`, `categories: Category[]`, `onSelectFilters: () => void`. Render `<WidgetFilterChips ... />` as a slim header row ABOVE `children` (inside the shell div, before `<div className="h-full w-full">{children}</div>`). Keep it visible regardless of `editMode`. The existing edit-mode drag/remove overlay stays absolutely positioned top-right; ensure the chip row doesn't collide (chips left-aligned, small top padding). Keep `data-widget-shell` anchor and aria contract.

**Run-to-pass:** GREEN; `tsc --noEmit` GREEN.

**Commit:** `feat(reports): widget shell renders effective filter chips`

---

### Task 8 — `requestedTab` deep-link on the popover (consume-and-clear handshake)

**Files:** `frontend/components/reports/WidgetEditorPopover.tsx`

**Why the old single-effect approach is broken (do not restore it):**
The previous design ("clear `requestedTab` on close", reset effect keyed on `[widget.id, requestedTab]`) has two race conditions:
- **(a) Dead second click.** Clicking a chip on the ALREADY-selected widget produces no state transition the second time — `requestedTab` is still `"filters"`, so the effect's deps don't change and the chip goes dead.
- **(b) Clobber-to-Data.** Keying the reset effect on `[widget.id, requestedTab]` means when `requestedTab` later clears (`"filters" → null`), the effect re-fires and resets the tab back to `"data"`, yanking the user off Filters.

The fix is a **consume-and-clear handshake with TWO separate effects**, plus a page that clears `requestedTab` the instant the popover consumes it (so every chip click is a genuine `null → "filters"` transition).

**Failing test:** `frontend/tests/components/reports/widget-editor-popover-requested-tab.test.tsx` (new):
```ts
it("opens on the Filters tab when requestedTab='filters'", () => {
  renderWithSWR(<WidgetEditorPopover widget={bar} canvasFilters={{}}
    anchorEl={document.body} requestedTab="filters" onTabConsumed={() => {}}
    onUpdate={() => {}} onClose={() => {}} />);
  expect(screen.getByRole("tab", { name: "Filters" })).toHaveAttribute("aria-selected", "true");
});
```
Plus the four behavioral cases:
1. **default:** WITHOUT `requestedTab`, the Data tab is selected (preserve #447 default).
2. **fresh widget + requested tab:** changing `widget.id` while passing `requestedTab="filters"` lands on Filters.
3. **(NEW) same-widget second click re-opens Filters:** render with `requestedTab="filters"` (Filters selected) + an `onTabConsumed` that clears the prop; user manually clicks the Data tab (now on Data); re-set `requestedTab="filters"` (simulating a second chip click on the same widget) → tab returns to Filters. This proves the dead-second-click race (a) is fixed.
4. **(NEW) clobber guard:** with a stable `widget.id`, transition `requestedTab` `"filters" → null` (the consume) and assert the tab does NOT reset to Data — it stays on whatever it is. This proves race (b) is fixed (the reset effect is NOT keyed on `requestedTab`).

**Run-to-fail:** RED.

**Implement:**
- Add `requestedTab?: TabKey` AND `onTabConsumed?: () => void` to `Props`. **Export `TabKey`** so the pages can type their `requestedTab` state.
- Keep the lazy init for the fresh-mount case: `const [tab, setTab] = useState<TabKey>(() => requestedTab ?? "data");`
- **Reset effect (widget-identity only):**
  ```ts
  useEffect(() => { setTab(requestedTab ?? "data"); }, [widget.id]);
  // eslint-disable-next-line react-hooks/exhaustive-deps -- intentional: reset
  // only on widget-identity change; requestedTab is read at switch time, not a dep.
  ```
  Do NOT add `requestedTab` to this effect's dep array (that is exactly what causes the clobber-to-Data race).
- **Honor-request effect (requestedTab only):**
  ```ts
  useEffect(() => {
    if (requestedTab) {
      setTab(requestedTab);
      onTabConsumed?.();
    }
  }, [requestedTab]); // onTabConsumed read in body, not a dep
  ```
  Acts ONLY when `requestedTab` is truthy, and calls `onTabConsumed` in the same body so the page clears it immediately → the next chip click is again a `null → "filters"` transition.
- Remove the old single `useEffect(() => setTab("data"), [widget.id])` and any "clear on close / clear on next tick" deliberation. The page's `closePopover` clearing `requestedTab` is kept only as a harmless belt-and-suspenders.

**Run-to-pass:** GREEN (all 6 cases); `tsc --noEmit` GREEN.

**Commit:** `feat(reports): popover honors a requested tab via consume-and-clear`

---

### Task 9 — Wire `[id]` page: fetch lookups, thread chips + requestedTab

**Files:** `frontend/app/reports/[id]/page.tsx`

**Failing test:** extend the page's existing test (find via `grep -rl "reports/\[id\]\|ReportEditorPage" tests/`) or add a focused integration test: clicking a widget's filter chip selects the widget and the popover opens on Filters. If full page render is heavy in tests, a lighter unit test on the chip→state path is acceptable; prefer reusing the existing page test harness.

**Run-to-fail:** RED.

**Implement:**
- Fetch accounts + categories once with SWR using the SAME keys the filters use (`/api/v1/accounts?for=reports-filter`, `/api/v1/categories?for=reports-filter`) so the cache is shared with `AccountFilter`/`CategoryPicker`. Default to `[]`.
- Add `const [requestedTab, setRequestedTab] = useState<TabKey | null>(null);` (import `TabKey` from the popover — Task 8 exports it).
- Add a handler `selectWidgetFilters(id)`: `setSelectedWidgetId(id); setRequestedTab("filters");`.
- In the **desktop `Canvas` `renderWidget` closure / `WidgetShell` render**, pass `widget={w}`, `canvasFilters`, `accounts`, `categories`, and **`onSelectFilters`** as follows (R5 — view-mode orphan-selection guard): pass the real handler `() => selectWidgetFilters(w.id)` ONLY when in edit mode, and a **no-op `() => {}` when NOT in edit mode**. Because the popover is gated on `editModeActive`, a chip click in view mode would otherwise draw a selection ring with no editor behind it. Concretely:
  ```tsx
  onSelectFilters={editModeActive ? () => selectWidgetFilters(w.id) : () => {}}
  ```
  Chips still RENDER in view mode (locked decision — they are status-is-data informational); they are just inert there.
- **Mobile stack — NO chips, NO change (architect decision).** The mobile read-only stack (~lines 813–817) renders `renderWidgetByType(w, canvasFilters, false)` directly, NOT wrapped in `WidgetShell`. Do **NOT** wrap it in `WidgetShell` and do **NOT** thread chip props into the stack. Mobile is read-only and shows no chips. (This removes the prior draft's contradictory "also pass the same to the mobile-stack branch / pass a no-op onSelectFilters in the stack" instruction.)
- Pass `requestedTab={requestedTab ?? undefined}` and `onTabConsumed={() => setRequestedTab(null)}` to `WidgetEditorPopover`. The popover's honor-request effect calls `onTabConsumed` the instant it consumes the request, so `requestedTab` returns to `null` immediately — making every chip click a fresh `null → "filters"` transition (fixes same-widget second click). ALSO clear `requestedTab` inside `closePopover` (`setRequestedTab(null)`) as harmless belt-and-suspenders.
- Update the empty-state copy that says `"Canvas filters (date, accounts, categories) apply to every widget..."` (~lines 778–781) → `"Canvas date applies to every widget; accounts and categories are per-widget."`

**Run-to-pass:** GREEN; `tsc --noEmit` GREEN.

**Commit:** `feat(reports): [id] editor wires filter chips + filters deep-link`

---

### Task 10 — Wire `new` (draft) page: same changes

**Files:** `frontend/app/reports/new/page.tsx`

**Failing test:** mirror Task 9's test against the draft page (it's always edit mode, so the popover gate is just `selectedWidget && anchorEl`).

**Run-to-fail:** RED.

**Implement:** same as Task 9 —
- fetch accounts/categories (shared SWR keys, default `[]`);
- add `requestedTab` state + `selectWidgetFilters`;
- thread `widget`/`canvasFilters`/`accounts`/`categories` into `WidgetShell`. The draft page is **always edit mode**, so `onSelectFilters` is the **real** handler `() => selectWidgetFilters(w.id)` (no view-mode no-op branch — there is no view mode here).
- pass `requestedTab` + `onTabConsumed={() => setRequestedTab(null)}` to the popover; clear on `closePopover` as belt-and-suspenders.
- **Fix the draft-page empty-state copy too (R4).** It is a SEPARATE string from the `[id]` page: `"Canvas filters (date, accounts, categories) apply to every widget."` at ~lines 289–292 of `new/page.tsx`. Change it to `"Canvas date applies to every widget; accounts and categories are per-widget."` (same wording as Task 9). Do not assume Task 9's edit covers this — the two copies are independent strings.
- The draft page uses `renderWidgetByType`; the chip header is in `WidgetShell` (rendered inline on this page in the `Canvas` closure), so no widget-kit change needed — just the `WidgetShell` props. The draft page has no mobile read-only stack, so no mobile decision applies here.

**Run-to-pass:** GREEN; `tsc --noEmit` GREEN.

**Commit:** `feat(reports): draft editor wires filter chips + filters deep-link`

---

### Task 11 — Rework the FilterEditor override-pill test

**Files:** `frontend/tests/components/reports/config/filter-editor-override-pill.test.tsx`

**Failing test / rework:** the account and category "override pill" cases can no longer fire — account/category are widget-only, so `isFieldOverridden` always returns false for them and no `override-pill` renders. Rewrite those four cases:
- account match/differ → pill NEVER shows (both assert `0` pills).
- category match/differ → pill NEVER shows.
- Keep (or add) the `date_range` override case as the surviving canvas-vs-widget override the pill still represents (add a date case if none exists, asserting the pill shows when widget date differs from canvas date and not when equal).
Also drop the canvas `account_ids`/`category_ids` props from these fixtures (those keys no longer exist on `CanvasFilters` → tsc error otherwise).

**Run-to-fail:** `docker compose exec -T frontend npx vitest run tests/components/reports/config/filter-editor-override-pill.test.tsx` — RED (old expectations + removed canvas keys).

**Implement** the rewrite.

**Run-to-pass:** GREEN; `tsc --noEmit` GREEN.

**Commit:** `test(reports): rework override-pill cases for date-only canvas`

---

### Task 12 — Repo-wide sweep for removed canvas keys + final gates

**Files:** any remaining references.

1. **Grep sweep:** `docker compose exec -T frontend grep -rn "account_ids\|category_ids" app components lib tests` — confirm every `canvas`-side reference is gone (widget-side `WidgetFilters.account_ids`/`category_ids` stays). Check templates/draft seed (`lib/reports/draft.ts`, `lib/reports/api.ts`, any template fixtures, `ReportTemplate.canvas_filters_json` shapes) for canvas `account_ids`/`category_ids` literals and remove them. Check `series.ts`, `csv.ts`, `use-widget-anchor.ts` for any canvas-filter coupling (unlikely).
2. **Confirm `pickList` is fully removed** (no stragglers): `docker compose exec -T frontend grep -rn "pickList" app components lib tests` returns nothing.
3. **Full vitest suite** (NOT just touched files — per the full-suite verification rule; a cross-file rename can regress elsewhere):
   `docker compose exec -T frontend npx vitest run`
4. **Type-check:** `docker compose exec -T frontend npx tsc --noEmit`
5. **Lint (CI gate):** `docker compose exec -T frontend npx eslint . --quiet`

All gates must be clean before opening the PR.

**Commit (if sweep changes anything):** `chore(reports): drop residual canvas account/category references`

---

## Must-flag list (call out in the PR description / review)

1. **No data migration** of saved reports. Old `canvas_filters_json` with `account_ids`/`category_ids` is silently ignored on read (keys no longer in `CanvasFilters`; the `as CanvasFilters` hydrate cast tolerates the extra keys; `resolveFilters` no longer reads them). This is a **behavior change** for any saved report that relied on canvas-level account/category scoping — those filters effectively vanish until the user re-adds them per-widget. Pre-launch, no users, acceptable; flag explicitly.
2. **Backend sends nothing new** — confirm the query endpoint still accepts a `canvas_filters_json` lacking those keys (it does; note it). `saveLayout`/`createReport` now persist a date-only canvas blob.
3. **Chip name resolution depends on shared SWR cache.** If a widget references a deleted/inactive account or category id, that id is dropped from the label (count fallback). Inactive accounts are already filtered out of `AccountFilter`'s selectable list, so a previously-selected-then-deactivated account could become a count-only chip — acceptable, note it.
4. **`stopPropagation` on chip click** is required so the chip's `onSelectFilters` doesn't also trigger `WidgetShell`'s `onSelect` (which would set `selectedWidgetId` without the Filters tab). Verify the chip wins.
5. **`requestedTab` consume-and-clear handshake.** Set on chip click (`null → "filters"`), consumed by the popover's honor-request effect which calls `onTabConsumed()` to clear it back to `null` in the same cycle. TWO separate effects in the popover: a reset effect keyed on `[widget.id]` ONLY (not `requestedTab`) and a honor-request effect keyed on `[requestedTab]`. This fixes both the dead-second-click race (every click is a genuine transition) and the clobber-to-Data race (the reset effect never re-fires when `requestedTab` clears). A subsequent plain widget click (no `requestedTab`) defaults to Data (#447 reset preserved). Covered by Task 8's two new cases + Task 9/10 tests.
6. **Mobile stack shows NO chips.** The mobile read-only branch renders `renderWidgetByType` directly, NOT through `WidgetShell`, so chips never appear there — intended (read-only, no popover). We do not add `WidgetShell` to the stack. No inert-chip / mobile-click concern exists.
7. **View-mode orphan-selection guard (`[id]` page).** In view mode the popover is gated on `editModeActive`, so `onSelectFilters` is passed as a no-op `() => {}` — a chip click in view mode draws no orphan selection ring with no editor behind it. Chips still render (informational). The draft page is always edit mode and passes the real handler.
8. **Currency symbol on the amount chip** is a bare `$` prefix (not per-account currency) — consistent with the deferred chart currency-symbol work (roadmap §1b). Note the simplification.
9. **Amount/date label formatting** has no shared formatter today (`grep` confirmed none in `lib/reports`); the new formatters live in `describe-filters.ts`. Don't duplicate `buildPresetRanges` — import it from `DatePresetChips`; don't duplicate the date inherit/override logic — import `pickDateRange` + `isFieldOverridden` from `resolve.ts`.
10. **`Canvas.tsx` is NOT touched** — the page `renderWidget` closure supplies the new `WidgetShell` props. **`pickList` is deleted** (only used by the two removed cascade branches). **`pickDateRange` is now exported** from `resolve.ts` (single source of truth for date inherit/override, reused by the describe helper).

---

## Self-Review

- **Does `describeWidgetFilters` mirror `resolveFilters`?** Both read `widget.config.filters`, both use the SAME `pickDateRange` for the effective date (now imported, not reimplemented), both gate accounts/categories on widget-only presence, both treat empty arrays as unset. The describe helper must NOT show a chip for a filter `resolveFilters` wouldn't emit (e.g. empty `tag_names`). Tested in Task 5.
- **Is the canvas→widget model shift total?** `CanvasFilters` shrinks (Task 1), `resolveFilters` drops the fallback + deletes `pickList` (Task 3), `CanvasFiltersBar` drops the pickers (Task 4), and the repo sweep (Task 12) catches stragglers in templates/draft/api. `FilterEditor` still renders the account/category PICKERS (widget-only editing) — that's correct and unchanged; only its override PILLS for those fields go dark.
- **Deep-link correctness (race-free):** chip → `onSelectFilters` → `selectedWidgetId` + `requestedTab="filters"` → popover honor-request effect sets Filters AND calls `onTabConsumed()` → page clears `requestedTab` to `null` immediately. Same-widget second click is again `null → "filters"` (no dead click). The reset effect is keyed on `[widget.id]` ONLY, so the `"filters" → null` consume never clobbers the tab back to Data. Plain widget click (no chip) → `onSelect` only → Data default (#447 preserved).
- **Both pages:** Tasks 9 + 10 apply equivalent wiring. The draft page is always-edit (real `onSelectFilters`); the `[id]` page gates the popover on `editModeActive` and passes a **no-op `onSelectFilters` in view mode** (R5) so chips stay informational without drawing an orphan selection ring. The mobile read-only stack on `[id]` shows **no chips** and is untouched.
- **Empty-state copy fixed in BOTH places (R4):** the string is duplicated independently in `[id]/page.tsx` (~778–781) and `new/page.tsx` (~289–292); Tasks 9 and 10 each fix their own copy.
- **No new dependency, one cohesive PR** — justified above.
- **CI gates are pre-PR:** full `vitest run`, `tsc --noEmit`, `eslint . --quiet` (Task 12).
- **Pre-launch hygiene:** no compat shims, no migration script (per locked feedback rules); `as CanvasFilters` hydrate cast tolerates legacy keys with no migration.
