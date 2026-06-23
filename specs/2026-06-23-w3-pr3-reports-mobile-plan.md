# W3 PR3 — Reports mobile read-only polish — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make Reports genuinely usable and good-looking on phones in read-only mode — fixing the two real gaps an investigation found, rather than redoing the parts already handled.

**Context — what's ALREADY done (do NOT redo):** the canvas already collapses to a single column on phones and forces read-only via `useIsSmallScreen` (`max-width:639px`); filter chips + date presets already `flex-wrap`; the page has no horizontal overflow; `TableWidget` already has an `overflow-auto` container; chart widgets use Recharts `ResponsiveContainer`. A mobile read-only stack test exists (`tests/app/reports-editor-page.test.tsx`).

**The TWO real gaps:**
1. **Crushed charts in the mobile stack.** `app/reports/[id]/page.tsx:962-971` renders each stacked widget in a bare `<div key>` with NO height. Recharts/Nivo charts use `height="100%"`, which collapses to ~0 inside an auto-height parent → charts are invisible/tiny on phones. (This now includes the new donut/area/bar and the Sankey.)
2. **Header action-bar overflow.** The page header (`app/reports/[id]/page.tsx` ~line 737) is a non-wrapping flex row with a breadcrumb + several action buttons; on ~320-390px it overflows horizontally.

**Architecture:** Give each mobile-stack widget an explicit, type-aware height so charts get a real canvas; let the header wrap/condense on small screens. Plus a small shared `useIsMobile` hook to de-duplicate the per-page `matchMedia` copies (touch-when-adjacent).

**Tech Stack:** Next.js 16 / React 19 / TS / Tailwind, Vitest.

## Global Constraints

- **No Off-Token Rule** — token classes only; bare hex / raw Tailwind palette colors are CI-blocked (`frontend/scripts/check-design-tokens.sh`).
- **Read-only on mobile stays read-only** — do not add edit affordances to the mobile stack.
- **Match the existing mobile-detection convention** — `window.matchMedia("(max-width: 639px)")` (Tailwind `sm`), SSR-safe (false until mounted), as in `useIsSmallScreen` in `page.tsx`.
- **Don't regress desktop** — all changes gated to small screens / additive responsive classes.
- **Frontend test discipline** — full vitest suite + `npx tsc --noEmit` green before done. Tests inside the container: `docker compose exec frontend npm test -- <path>`.
- **No AI attribution** in commits.

---

### Task 1: Give mobile-stack widgets a usable height

**Files:**
- Modify: `frontend/app/reports/[id]/page.tsx` (the `isSmallScreen` stack branch ~lines 962-971)
- Test: extend `frontend/tests/app/reports-editor-page.test.tsx` (the existing mobile-stack test)

**Interfaces:**
- Produces: a pure helper `mobileStackHeight(widget: Widget): number | undefined` (px height for the stack wrapper; `undefined` = natural height for content widgets). Export it for testing.

- [ ] **Step 1: Write the failing test.** In the mobile-stack section of `reports-editor-page.test.tsx`, add a test that the stack wrapper for a chart widget (e.g. a `bar`/`area`/`pie`/`sankey`) gets a definite pixel height (so the chart can render), while a `kpi` widget does NOT get a forced tall height. Prefer testing the exported `mobileStackHeight` helper directly with representative widgets, plus one DOM assertion that a charted stack item carries an inline height style.
```tsx
import { mobileStackHeight } from "@/app/reports/[id]/page";
it("gives chart widgets a definite mobile height and leaves KPI/table natural", () => {
  expect(mobileStackHeight({ type: "bar", grid: { x:0,y:0,w:6,h:4 } } as any)).toBeGreaterThanOrEqual(220);
  expect(mobileStackHeight({ type: "sankey", grid: { x:0,y:0,w:8,h:5 } } as any)).toBeGreaterThanOrEqual(260);
  expect(mobileStackHeight({ type: "kpi", grid: { x:0,y:0,w:3,h:2 } } as any)).toBeUndefined();
  expect(mobileStackHeight({ type: "table", grid: { x:0,y:0,w:6,h:6 } } as any)).toBeUndefined();
});
```
- [ ] **Step 2: Run it, verify it fails.** Run: `docker compose exec frontend npm test -- tests/app/reports-editor-page.test.tsx`. Expected: FAIL (`mobileStackHeight` not exported).
- [ ] **Step 3: Implement the helper + apply it.** Add near `orderWidgetsForStack`:
```ts
// Chart widgets need a definite height for Recharts/Nivo height="100%" to
// render in the mobile stack (the wrapper is otherwise auto-height → ~0).
// KPI and table size to their content, so they stay natural-height.
const CHART_STACK_TYPES = new Set<WidgetType>([
  "bar", "stacked_bar", "line", "area", "pie", "sparkline", "sankey",
]);
export function mobileStackHeight(widget: Widget): number | undefined {
  if (!CHART_STACK_TYPES.has(widget.type)) return undefined;
  const base = widget.grid.h * 56; // ~rowHeight; taller widgets stay taller
  return Math.min(Math.max(base, widget.type === "sankey" ? 260 : 220), 460);
}
```
Then in the stack render, apply it:
```tsx
{orderWidgetsForStack(layout.widgets).map((w) => {
  const h = mobileStackHeight(w);
  return (
    <div key={w.id} style={h ? { height: h } : undefined}>
      {renderWidgetByType(w, canvasFilters, false, currency)}
    </div>
  );
})}
```
- [ ] **Step 4: Run test, verify it passes.** Then `docker compose exec frontend npx tsc --noEmit`.
- [ ] **Step 5: Commit.**
```bash
git add frontend/app/reports/[id]/page.tsx frontend/tests/app/reports-editor-page.test.tsx
git commit -m "fix(reports): give mobile-stack chart widgets a usable height"
```

---

### Task 2: Header action bar wraps/condenses on small screens

**Files:**
- Modify: `frontend/app/reports/[id]/page.tsx` (header ~line 737)
- Test: extend `frontend/tests/app/reports-editor-page.test.tsx`

- [ ] **Step 1: READ the header JSX** (~lines 737-915) to inventory the action buttons and the breadcrumb. Identify the left (breadcrumb/title) and right (action buttons) groups.
- [ ] **Step 2: Write the failing test** asserting the header container allows wrapping on small screens (e.g. the header row has `flex-wrap` and the action group has `flex-wrap`), so buttons don't overflow. A DOM/class assertion is acceptable here (this is layout): assert the header element carries `flex-wrap` and a `gap` class.
- [ ] **Step 3: Implement.** Add `flex-wrap gap-y-2` to the header row and the right-side action group so they wrap below the breadcrumb on narrow widths instead of overflowing. Reduce horizontal gaps on mobile (`gap-2` → `gap-1.5`) where needed. Keep desktop unchanged (it already fits on one row). Do NOT hide actions behind a menu (out of scope) — wrapping is sufficient and keeps all read-only actions reachable.
- [ ] **Step 4: Run the test + full suite + tsc.** Run: `docker compose exec frontend npm test -- tests/app/reports-editor-page.test.tsx` then `docker compose exec frontend npx tsc --noEmit`. Expected: green.
- [ ] **Step 5: Commit.**
```bash
git add frontend/app/reports/[id]/page.tsx frontend/tests/app/reports-editor-page.test.tsx
git commit -m "fix(reports): wrap report header actions on small screens"
```

---

### Task 3: Extract shared `useIsMobile` hook (de-dupe) + verification

**Files:**
- Create: `frontend/lib/hooks/use-is-mobile.ts`
- Modify: `frontend/app/reports/[id]/page.tsx` (use the shared hook in place of the local `useIsSmallScreen`)
- Test: `frontend/tests/lib/hooks/use-is-mobile.test.ts` (create)

**Interfaces:**
- Produces: `export function useIsMobile(): boolean` — `window.matchMedia("(max-width: 639px)")`, SSR-safe (false until mounted), subscribes to `change`.

- [ ] **Step 1: Write the failing test** for `useIsMobile` mirroring how the existing mobile-stack test stubs `matchMedia` (read its `mockMatchMedia` helper). Assert it returns the match value and updates on `change`.
- [ ] **Step 2: Run it, verify it fails.**
- [ ] **Step 3: Implement** `useIsMobile` (move the body of the local `useIsSmallScreen` into the shared hook, keep the `(max-width: 639px)` query). In `page.tsx`, replace the local `useIsSmallScreen` definition + call with the imported `useIsMobile` (keep the call-site variable name `isSmallScreen` to minimize churn, or rename consistently). Do NOT change behavior.
- [ ] **Step 4: Run test + FULL suite + tsc.** Run: `docker compose exec frontend npm test` and `docker compose exec frontend npx tsc --noEmit`. Expected: all green (the existing mobile-stack test still passes through the shared hook).
- [ ] **Step 5: Design-token gate.** Run: `docker compose exec frontend bash scripts/check-design-tokens.sh`. Expected: exit 0.
- [ ] **Step 6: Commit.**
```bash
git add frontend/lib/hooks/use-is-mobile.ts frontend/app/reports/[id]/page.tsx frontend/tests/lib/hooks/use-is-mobile.test.ts
git commit -m "refactor(reports): shared useIsMobile hook for the mobile read-only stack"
```

---

### Manual visual verification (after the build, before PR)

With the stack running, open `/reports` in a narrow viewport (DevTools 360px + 390px):
- Each chart widget (donut, area, bar, stacked bar, line, sparkline, **Sankey**) renders at a real, readable height in the single-column stack — not crushed.
- The header breadcrumb + actions wrap cleanly; no horizontal page scroll.
- Tables scroll within their card; filter chips/date presets wrap.
- Rotate/resize across the 639px boundary: the read-only stack ↔ desktop grid flip still works.

## Out of scope (deferred — note in PR)
- Date From/To input stacking on <360px (native pickers are acceptable today).
- Table pagination compaction on mobile.
- Mobile hamburger/overflow menu for header actions (wrapping is enough for read-only).
- Touch drag/resize/add (that's W4 gridstack).

## Self-review (done)
- **Spec coverage:** the two real gaps (stack heights T1, header overflow T2) + the de-dupe refactor (T3) the investigation flagged. The already-solved items (single-column collapse, chip wrapping, table overflow, ResponsiveContainer) are deliberately not re-touched.
- **Placeholders:** helper code + test assertions are concrete; exact header class edits depend on reading the current JSX (Task 2 Step 1), which is the correct order.
- **Type consistency:** `mobileStackHeight`, `CHART_STACK_TYPES`, `useIsMobile` named consistently; `WidgetType` reused.
