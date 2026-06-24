# Proportional Layout Pass (Tier A)

**Date:** 2026-06-24
**Status:** Approved (brainstorm complete — operator said "just go")
**Context:** W4 Phase 0 widened the global content container from `max-w-screen-xl` (1280px) to `max-w-[1760px]` (#478, open). That exposed pages designed for 1280px: full-width charts now span ~1760px and look stretched; forms with internal caps (Settings Profile/Security `max-w-lg`) look marooned in white space. This pass redesigns the affected pages into **proportional, contained, multi-column layouts** — same look & feel, never stretched. **No drag/resize here** (that is Tier B / W4 customizable canvas, a separate effort). This is the "Tier A" decision from the 2026-06-24 brainstorm → [[project_reprioritization_2026_06_22]].

## Guiding principle

**Stretch a wide data *table* = good; stretch a *chart* or a lone *form field* = bad.** So:
- **Charts** never span the full 1760px alone — they live in a contained card within a multi-column grid (chart beside a summary/details, or capped span).
- **KPI/stat tiles** use a responsive grid (already mostly do).
- **Forms** get a proportional multi-column arrangement at a sensible width, not a 512px column nor a 1760px-wide single input.
- **Wide data tables** (Transactions, Recurring, Accounts, Admin) are LEFT AS-IS — they use full width well.

## Design-system constraints

- **No Off-Token Rule** — token classes only; CI-gated. `max-w-[...]`/`grid-cols-*`/`gap-*` are size/layout utilities (allowed); no raw color.
- **Body-Is-Sm**, card primitives from `lib/styles.ts` (`card`, `cardHeader`, `cardTitle`), brass/sidebar rules — unchanged.
- **Frontend verify MUST include `npm run lint`** (eslint `no-explicit-any` is CI-gated, not caught by `tsc`/tests) → [[reference_eslint_ci_gate_misses]].
- **No AI attribution** in commits or PR bodies → [[feedback_no_ai_attribution]].
- Built/tested at the **1760px width** (stack stays on the #478 branch base) so proportions are judged at the real target width.

## Shared foundation (Slice A, built once, reused)

**`StatCard` component** — `frontend/components/ui/StatCard.tsx`. KPIs are hand-rolled in every page today (Budgets, Forecast Plans, Dashboard each inline their own stat divs). One primitive:
```
interface StatCardProps {
  label: string;            // e.g. "TOTAL BUDGET" (rendered uppercase-label style)
  value: ReactNode;         // formatted amount/number
  valueClassName?: string;  // for status color, e.g. text-success / text-danger (tokens only)
  sub?: ReactNode;          // optional secondary line, e.g. "Actual: 0.00"
  badge?: ReactNode;        // optional status pill
}
```
Uses `card` + `cardTitle` + label/value typography matching today's tiles (capture the current Budgets KPI styling so the look is unchanged). A unit test asserts label/value/sub render and `valueClassName` is applied. This replaces the inline KPI markup on the pages below.

No other new abstraction — proportional grids are plain Tailwind (`grid`, `lg:grid-cols-*`, `gap-*`), matching existing patterns.

---

## Per-page layouts

### Budgets — `frontend/app/budgets/page.tsx` (Slice A, the template)
- **Row 1 — KPIs:** `grid grid-cols-1 sm:grid-cols-3 gap-4` of three `StatCard`s (Total Budget, Total Spent, Remaining — Remaining keeps its `text-success`). (Already a 3-col grid; just swap to `StatCard`.)
- **Row 2 — chart + details side-by-side (the de-stretch):** `grid grid-cols-1 xl:grid-cols-5 gap-6`:
  - **Budget Overview chart card** — `xl:col-span-3` (≈60%). The horizontal bar chart now renders at ~1000px, not 1760px.
  - **Details card** — `xl:col-span-2` (≈40%) — the per-category list (amounts, %, Transfer/Edit/Remove). A narrow list reads fine at ~40%.
- Mobile/`<xl`: single column (chart then details), unchanged behavior.

### Forecast Plans — `frontend/app/forecast-plans/ForecastPlansClient.tsx` (Slice B)
- **Row 1 — KPIs:** `grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4` of four `StatCard`s (Planned Income/Expenses/Net, Actual Net — keep their existing value colors + the "Actual: …" `sub`).
- **Row 2 — chart contained:** the "Planned vs Actual" chart card capped so it doesn't span alone — `grid grid-cols-1 xl:grid-cols-3 gap-6` with the chart card `xl:col-span-2` and a compact legend/summary card (`xl:col-span-1`) beside it (or, if no natural summary, cap the chart card at `xl:col-span-2` and leave the third column for the All/Expenses/Income view toggle + counts).
- **Row 3 — detail tables:** the Income/Expense category tables stay **full width** (tables use width well). The view-filter tabs (All/Expenses/Income) stay above them.
- Header/period-nav/controls unchanged.

### Settings — `frontend/app/settings/page.tsx` + tab pages (Slice C)
**Root bug:** Profile + Security tab content is wrapped in `max-w-lg` (512px) while Organization + AI Providers tabs fill width → jarring inconsistency. **Normalize UP** (operator liked the wider Org/AI tabs):
- Remove the `max-w-lg` cap from Profile + Security.
- **Profile tab** → `grid grid-cols-1 lg:grid-cols-2 gap-6`: left column = identity card + Edit-Profile form; right column = Dashboard-Tour card (+ room for future cards). Proportional, fills width, form fields no longer a lone stretched input.
- **Security tab** → same 2-col arrangement for its cards (MFA / password / sessions).
- Confirm Notifications tab also reads proportional at width (it's a list of toggles — cap or 2-col as fits; match the others).
- **Org + AI Providers** already fill width — leave, just verify they sit under the same outer wrapper as the now-normalized tabs.
- Form inputs themselves get a sane max within their column (don't let a single text input span the whole column) — e.g. inputs `max-w-md` inside the form, consistent with the existing field styling.

### Categories — `frontend/app/categories/page.tsx` (Slice D)
- Master-category list → responsive multi-column **`grid grid-cols-1 lg:grid-cols-2 gap-4`** of master cards, each listing its subcategories. De-stretches the single full-width list.
- **dnd caveat:** Categories uses dnd-kit for reorder / move-sub-between-masters. READ the dnd wiring first — if a 2-col grid breaks cross-master drag, fall back to a **centered capped single column** (`max-w-3xl mx-auto`) instead (still de-stretched, dnd intact). The implementer picks based on what keeps dnd working; document the choice.

### Left as-is (verify only, no change)
Transactions, Recurring, Accounts, Admin (orgs/users/audit/analytics/rate-limit/roles/announcements) — wide data tables, already use full width well. No proportional change; just a visual sanity check at 1760px.

---

## Slices (each its own PR, stacked on #478 so they build on the wide canvas)

- **Slice A** — `StatCard` primitive + **Budgets** (proves the template).
- **Slice B** — **Forecast Plans**.
- **Slice C** — **Settings** normalize (all tabs).
- **Slice D** — **Categories** (with dnd caveat) + the leave-as-is verification sweep.

Merge order at the end: **#478 (width) first, then Slices A→D** (rebased onto main as each lands), so prod never shows the wide-but-disproportionate intermediate state — the "wide + proportional" release lands coherently.

## Out of scope (explicit)
- Drag/resize/move (Tier B / W4 customizable dashboard — separate spec `2026-06-23-w4-customizable-dashboard-design.md`).
- Touching wide data tables (they're fine).
- New chart types or data changes — pure layout.
- Dashboard page (it gets the Tier-B canvas treatment, not this static pass).

## Per-slice acceptance
- The page reads proportional at 1760px (no lone full-width chart; no marooned narrow form); identical at mobile widths (single-column stack); look & feel of cards unchanged.
- `tsc --noEmit` clean, `npm run lint` 0 errors, design-token gate green, full suite green.
- Manual visual check at 1280/1760px + a mobile width.

## Review
Each slice via subagent-driven-development + per-task review + a whole-branch review, `npm run lint` in the verify set. Visual verification per page (the whole point is how it looks).
