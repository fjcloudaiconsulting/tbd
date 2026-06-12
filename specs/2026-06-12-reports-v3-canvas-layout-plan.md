# Reports v3 — Canvas Layout Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make the report canvas honor widget positions/sizes literally — no auto-compaction, no auto-movement, and no spurious "unsaved changes" on load.

**Architecture:** `frontend/components/reports/Canvas.tsx` wraps react-grid-layout's `WidthProvider(Responsive)`. Two changes: (1) set `compactType={null}` + `preventCollision` so widgets stay exactly where placed and never auto-pack; (2) guard `handleLayoutChange` so it only propagates a change when a widget's grid actually differs — this swallows the mount-time emission (from WidthProvider's first measure) and any no-op emission, which today flips the editor to `dirty=true` on load. The grid-diff logic is extracted into pure, unit-testable helpers. Persistence is unchanged (explicit Save button already; layout changes just set `dirty`).

**Tech Stack:** Next.js 16 / React 19 / TypeScript, react-grid-layout, vitest. **Frontend test rule:** run the FULL `vitest run` suite, never a single file (per `reference_frontend_full_suite_verification`).

**Out of scope (next phase):** the ConfigRail→popover rebuild that eliminates the settings-panel-open reflow (that's the Phase-4 editor work). This PR does not touch ConfigRail or the page flex container.

**Reference (current code, read first):**
- `frontend/components/reports/Canvas.tsx` — `rgLayout` useMemo (~50-62), `<ResponsiveGridLayout>` JSX (~80-97), `handleLayoutChange` (~64-76). Today: no `compactType`/`preventCollision`; `handleLayoutChange` maps every emission into widgets and calls `onLayoutChange` unconditionally.
- `frontend/app/reports/[id]/page.tsx` — `updateLayout` (~403-406) sets `dirty=true`; `hydrateFromReport` (~329-340) resets `dirty=false` on load.
- `frontend/tests/app/reports-editor-page.test.tsx` — stubs Canvas (jsdom can't measure width); asserts add/save/filter flows. Must stay green.

---

## File Structure
- **Create** `frontend/lib/reports/layout.ts` — pure helpers `widgetsFromLayout(items, rglItems)` and `gridChanged(prev, next)`.
- **Modify** `frontend/components/reports/Canvas.tsx` — use the helpers in `handleLayoutChange` (early-return on no change) + add `compactType={null}` and `preventCollision`.
- **Create** `frontend/tests/lib/reports/layout.test.ts` — unit tests for the helpers.
- **Create/extend** `frontend/tests/components/reports/Canvas.test.tsx` — mock react-grid-layout to assert `onLayoutChange` fires only on a real grid change, not on a mount-time/no-op emission.

---

### Task 1: Pure grid-diff helpers (TDD)

**Files:**
- Create: `frontend/lib/reports/layout.ts`
- Test: `frontend/tests/lib/reports/layout.test.ts`

- [ ] **Step 1: Write the failing tests**

```typescript
// frontend/tests/lib/reports/layout.test.ts
import { describe, it, expect } from "vitest";
import { widgetsFromLayout, gridChanged } from "@/lib/reports/layout";
import type { Widget } from "@/lib/reports/types";

const w = (id: string, x: number, y: number, ww = 4, h = 4): Widget =>
  ({ id, type: "kpi", title: id, grid: { x, y, w: ww, h }, config: {} } as unknown as Widget);

describe("widgetsFromLayout", () => {
  it("applies new x/y/w/h from the rgl items, keyed by id", () => {
    const items = [w("a", 0, 0), w("b", 0, 4)];
    const next = [
      { i: "a", x: 2, y: 0, w: 4, h: 4 },
      { i: "b", x: 0, y: 4, w: 6, h: 5 },
    ];
    const out = widgetsFromLayout(items, next);
    expect(out[0].grid).toEqual({ x: 2, y: 0, w: 4, h: 4 });
    expect(out[1].grid).toEqual({ x: 0, y: 4, w: 6, h: 5 });
  });

  it("leaves a widget untouched when no rgl item matches its id", () => {
    const items = [w("a", 0, 0)];
    const out = widgetsFromLayout(items, [{ i: "ghost", x: 9, y: 9, w: 1, h: 1 }]);
    expect(out[0].grid).toEqual({ x: 0, y: 0, w: 4, h: 4 });
  });
});

describe("gridChanged", () => {
  it("is false when every widget's grid is identical (mount / no-op emission)", () => {
    const items = [w("a", 0, 0), w("b", 0, 4)];
    expect(gridChanged(items, widgetsFromLayout(items, [
      { i: "a", x: 0, y: 0, w: 4, h: 4 },
      { i: "b", x: 0, y: 4, w: 4, h: 4 },
    ]))).toBe(false);
  });

  it("is true when any x/y/w/h moved (real drag/resize)", () => {
    const items = [w("a", 0, 0)];
    expect(gridChanged(items, [{ ...items[0], grid: { x: 1, y: 0, w: 4, h: 4 } } as Widget])).toBe(true);
  });
});
```

- [ ] **Step 2: Run to verify fail**

Run: `docker compose exec -T frontend npx vitest run tests/lib/reports/layout.test.ts`
Expected: FAIL — module `@/lib/reports/layout` not found.

- [ ] **Step 3: Implement**

```typescript
// frontend/lib/reports/layout.ts
import type { Widget } from "@/lib/reports/types";

/** Minimal shape of a react-grid-layout item (the fields we read). */
export interface RglItem {
  i: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

/** Apply react-grid-layout positions back onto widgets, keyed by id.
 *  Widgets with no matching rgl item are returned unchanged. */
export function widgetsFromLayout(items: Widget[], rglItems: RglItem[]): Widget[] {
  const byId = new Map(rglItems.map((l) => [l.i, l]));
  return items.map((wgt) => {
    const l = byId.get(wgt.id);
    if (!l) return wgt;
    return { ...wgt, grid: { x: l.x, y: l.y, w: l.w, h: l.h } };
  });
}

/** True only when at least one widget's grid x/y/w/h actually differs.
 *  Used to ignore react-grid-layout's mount-time and no-op emissions so
 *  loading a report does not spuriously mark the editor dirty. */
export function gridChanged(prev: Widget[], next: Widget[]): boolean {
  if (prev.length !== next.length) return true;
  const byId = new Map(prev.map((p) => [p.id, p.grid]));
  for (const n of next) {
    const g = byId.get(n.id);
    if (!g) return true;
    if (g.x !== n.grid.x || g.y !== n.grid.y || g.w !== n.grid.w || g.h !== n.grid.h) {
      return true;
    }
  }
  return false;
}
```

- [ ] **Step 4: Run to verify pass**

Run: `docker compose exec -T frontend npx vitest run tests/lib/reports/layout.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/lib/reports/layout.ts frontend/tests/lib/reports/layout.test.ts
git commit -m "feat(reports): pure grid-diff helpers for literal canvas layout"
```

---

### Task 2: Wire the guard + literal-layout props into Canvas

**Files:**
- Modify: `frontend/components/reports/Canvas.tsx`
- Test: `frontend/tests/components/reports/Canvas.test.tsx`

- [ ] **Step 1: Write the failing test** (mock react-grid-layout so we can drive `onLayoutChange`)

```tsx
// frontend/tests/components/reports/Canvas.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import type { ComponentProps } from "react";

// Capture the props react-grid-layout receives + expose its onLayoutChange.
let captured: any = null;
vi.mock("react-grid-layout", () => {
  const React = require("react");
  const Responsive = (props: any) => {
    captured = props;
    return React.createElement("div", { "data-testid": "rgl" }, props.children);
  };
  return { Responsive, WidthProvider: (C: any) => C, default: { Responsive } };
});

import Canvas from "@/components/reports/Canvas";
import type { LayoutJson } from "@/lib/reports/types";

const layout: LayoutJson = {
  version: 1,
  widgets: [
    { id: "a", type: "kpi", title: "A", grid: { x: 0, y: 0, w: 4, h: 4 }, config: {} },
  ] as any,
};

function renderCanvas(onLayoutChange = vi.fn()) {
  render(
    <Canvas layout={layout} editMode onLayoutChange={onLayoutChange} renderWidget={() => <div>w</div>} />,
  );
  return onLayoutChange;
}

describe("Canvas literal layout", () => {
  it("passes compactType=null and preventCollision to react-grid-layout", () => {
    renderCanvas();
    expect(captured.compactType).toBeNull();
    expect(captured.preventCollision).toBe(true);
  });

  it("does NOT call onLayoutChange for a mount/no-op emission (same grid)", () => {
    const cb = renderCanvas();
    captured.onLayoutChange([{ i: "a", x: 0, y: 0, w: 4, h: 4 }]); // identical → ignored
    expect(cb).not.toHaveBeenCalled();
  });

  it("DOES call onLayoutChange when a widget actually moves", () => {
    const cb = renderCanvas();
    captured.onLayoutChange([{ i: "a", x: 3, y: 0, w: 4, h: 4 }]); // moved → propagate
    expect(cb).toHaveBeenCalledTimes(1);
    expect(cb.mock.calls[0][0].widgets[0].grid.x).toBe(3);
  });
});
```

Note: adapt the mock to however the file imports RGL (`import { Responsive, WidthProvider } from "react-grid-layout"`). If `Canvas` is a named vs default export, match it. Verify against the real file before finalizing.

- [ ] **Step 2: Run to verify fail**

Run: `docker compose exec -T frontend npx vitest run tests/components/reports/Canvas.test.tsx`
Expected: FAIL — `compactType` undefined / `onLayoutChange` called on no-op.

- [ ] **Step 3: Implement** — edit `frontend/components/reports/Canvas.tsx`

Import the helpers:
```tsx
import { widgetsFromLayout, gridChanged } from "@/lib/reports/layout";
```

Replace the body of `handleLayoutChange` so it early-returns on no change:
```tsx
function handleLayoutChange(next: Layout[]) {
  if (!editMode) return;
  const updated = widgetsFromLayout(items, next);
  if (!gridChanged(items, updated)) return; // swallow mount-time / no-op emissions
  onLayoutChange({ ...layout, widgets: updated });
}
```

Add the two props to `<ResponsiveGridLayout>`:
```tsx
  compactType={null}
  preventCollision
```
(place them alongside the existing `cols`/`rowHeight` props).

- [ ] **Step 4: Run the new test + verify pass**

Run: `docker compose exec -T frontend npx vitest run tests/components/reports/Canvas.test.tsx`
Expected: PASS (all three).

- [ ] **Step 5: Commit**

```bash
git add frontend/components/reports/Canvas.tsx frontend/tests/components/reports/Canvas.test.tsx
git commit -m "fix(reports): honor widget positions literally, no spurious dirty on load"
```

---

### Task 3: Full-suite verification + type-check + PR

- [ ] **Step 1: TypeScript type-check**

Run: `docker compose exec -T frontend npx tsc --noEmit`
Expected: no new errors.

- [ ] **Step 2: FULL vitest suite** (NOT a single file — load-bearing per the project rule)

Run: `docker compose exec -T frontend npx vitest run`
Expected: all green, including `tests/app/reports-editor-page.test.tsx` (the existing add/save/filter flows must be unaffected — this change only suppresses no-op emissions and adds two RGL props).

- [ ] **Step 3: Open the PR**

Branch `feat/reports-v3-canvas`. Push and open PR.
Title: `fix(reports): canvas honors widget position/size, no auto-compaction (Reports v3 phase 2)`
Body: concise, no test-plan section, no AI attribution. Note it fixes the literal-placement + spurious-dirty bugs; the settings-panel-open reflow is fixed with the popover editor in the next phase.

---

## Self-Review

**Spec coverage:** literal positions (`compactType={null}` + `preventCollision`) ✓ Task 2 · no spurious dirty on load (mount-emission guard via `gridChanged`) ✓ Tasks 1-2 · persistence unchanged (explicit Save already) ✓ (no change needed). The ConfigRail-reflow is explicitly deferred to the popover phase and called out in scope.

**Placeholder scan:** none — full code in every step. The one runtime unknown (exact RGL import style / Canvas export shape) is flagged as a verify-against-the-real-file step, not left silent.

**Type consistency:** `RglItem`/`widgetsFromLayout`/`gridChanged` defined in Task 1, consumed by name in Task 2. `Widget` type imported from `@/lib/reports/types` in both. The `gridChanged` signature (prev: Widget[], next: Widget[]) matches its Task-2 call site `gridChanged(items, updated)`.
