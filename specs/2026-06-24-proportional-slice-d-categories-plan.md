# Proportional Pass — Slice D (Categories) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** De-stretch the Categories page at the 1760px width: the single full-width column of master-category cards becomes a proportional 2-column grid, with drag-and-drop (move a subcategory onto a master) fully intact.

**Architecture:** Categories renders `masters.map(...)` inside a `<div className="space-y-4">` (line 536) wrapped in a dnd-kit `DndContext` (line 530). dnd-kit droppables are registered by id, NOT DOM position, so a CSS grid does not affect drag targeting. Change the single-column container to a 2-col grid; everything else (drag, move-preview, edit mode) is unchanged. The last slice of the proportional pass.

**Tech Stack:** Next.js 16 / React 19 / TS / Tailwind v4 / dnd-kit, Vitest.

## Global Constraints

- **No Off-Token Rule** — token color classes only; CI-gated. `grid`/`gap`/`items-start`/`max-w-[*]` are layout utils (allowed).
- **Frontend verify INCLUDES `npm run lint`** (eslint `no-explicit-any` CI-gated; not caught by tsc/tests) → [[reference_eslint_ci_gate_misses]]. No `as any`.
- **No AI attribution** in commits or PR body → [[feedback_no_ai_attribution]].
- **dnd MUST stay working** — the existing `frontend/tests/app/categories-drag-drop.test.tsx` is the proof; it must pass unchanged.
- Tests in the frontend container: `docker compose exec frontend <cmd>`. Branch `feat/proportional-d-categories` (off main; has #478 width + #479/#480/#481 shipped).
- A `transactions-page` test has a known pre-existing flake — if ONLY that fails, it's not a regression (confirm in isolation).

---

### Task 1: Categories master list → proportional 2-col grid

**Files:**
- Modify: `frontend/app/categories/page.tsx` (the master-list container, line 536)
- Test: add `frontend/tests/app/categories-layout.test.tsx` (mirror an existing categories test's mock setup)

- [ ] **Step 1: READ `frontend/app/categories/page.tsx`** lines ~525-650: the `DndContext` (530), the `<div className="space-y-4">` master-list container (536), `masters.map((master) => …)` rendering each master card with its `childrenMap` sub-rows, and the search/edit-mode controls above. Confirm masters are droppables (drag a sub onto a master) — so a grid won't break targeting.

- [ ] **Step 2: Change the container to a 2-col grid.** Line 536: `<div className="space-y-4">` → `<div className="grid grid-cols-1 lg:grid-cols-2 gap-4 items-start">`. (`items-start` so a tall master card with many subs doesn't vertically stretch its neighbor — the lesson from Slice C.) Keep everything inside each master card unchanged. The `space-y-*` inside individual cards (e.g. the sub list `space-y-0.5` at ~600) stays.

- [ ] **Step 3: Verify dnd is intact.** Run the existing drag-drop suite: `docker compose exec frontend npm test -- tests/app/categories-drag-drop.test.tsx`. Expected: PASS unchanged (dnd-kit targets by droppable id, not layout). If it fails, the grid broke targeting — STOP and report (fall back to a centered single column `max-w-3xl mx-auto` only if dnd genuinely breaks; document why).

- [ ] **Step 4: Write a layout test.** Create `frontend/tests/app/categories-layout.test.tsx` (reuse the mock/auth/api setup from `categories-c2-edit-mode.test.tsx` or `categories-drag-drop.test.tsx` — READ one first). Seed ≥2 master categories; assert the master cards render inside a container whose className includes `lg:grid-cols-2` (target it via the masters' common ancestor, or add a `data-testid="categories-master-grid"` to the line-536 div and assert `getByTestId(...).className` contains `lg:grid-cols-2`). Prefer adding the `data-testid` for a stable selector. Assert ≥2 master cards are present.

- [ ] **Step 5: Typecheck + lint + full suite.** `docker compose exec frontend npx tsc --noEmit && docker compose exec frontend npm run lint && docker compose exec frontend npm test`. tsc clean, lint 0 errors, suite green (mind the known transactions-page flake — confirm in isolation if it's the only failure). The categories-drag-drop, categories-mobile-pass, and categories-c2-edit-mode suites must all still pass.

- [ ] **Step 6: Commit.**
```bash
git add frontend/app/categories/page.tsx frontend/tests/app/categories-layout.test.tsx
git commit -m "feat(categories): proportional 2-col master grid (dnd intact)"
```

---

### Task 2: Visual verification
- [ ] **Step 1.** `/categories` at 1760px: master cards render two-up (no longer one stretched full-width column); each card lists its subcategories; the layout reads proportional.
- [ ] **Step 2.** **Drag a subcategory onto a master in the OTHER column** — confirm the move-preview + move still works across columns (the key dnd check the automated test can't fully prove visually).
- [ ] **Step 3.** Mobile (390px): single column; edit mode + search still work; no horizontal scroll.

## Out of scope
- Reordering masters / changing the move semantics — layout only.
- Touching the sub-row internals, edit mode, or the add-master/add-sub flows.

## Self-review (done)
- **Spec coverage:** the Proportional Pass spec's Categories slice — 2-col master grid, dnd preserved (the spec's caveat resolves to "2-col is safe" because dnd-kit targets by id). `items-start` carries the Slice-C lesson. Last slice → the pass is complete after this.
- **Placeholders:** the container line + dnd mechanism are read from the live file (Step 1) before editing; the layout test reuses an existing categories test's setup (named).
- **Type consistency:** N/A (layout-only; no new API). Optional `data-testid` is the only addition.
