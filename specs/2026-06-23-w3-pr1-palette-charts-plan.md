# W3 PR1 — Palette + Chart Styling Refresh — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the flat, partly-grey 5-color chart palette with a rich 8-hue "Brass Harmony" categorical scale and ship three visual upgrades (donut + center total, gradient smooth area, rounded palette bars), making Reports charts look Monarch-grade while staying token-pure.

**Architecture:** Add a dedicated 8-hue categorical theme scale (`--theme-cat-1..8`, light+dark) in `globals.css`, alias `--color-chart-1..8` to it, and consolidate the six duplicated per-widget color arrays into one `CHART_SERIES` constant. Then upgrade the Pie/Area/Bar/StackedBar chart components and amend DESIGN.md to carve out the chart-palette exception to The One Brass Rule.

**Tech Stack:** Next.js 16 / React 19 / TypeScript, Recharts 3.8, Tailwind (token-gated), Vitest.

## Global Constraints

- **No Off-Token Rule** — every color is a `--color-*` token in `frontend/app/globals.css`; bare hex / raw Tailwind palette colors are CI-blocked by `frontend/scripts/check-design-tokens.sh`. Chart colors referenced as `var(--color-chart-N)`.
- **The One Brass Rule** still governs chrome; the amendment is narrow (categorical chart series only).
- **WCAG 2.2 AA** — color never the only signal; light-theme hues deepened for contrast on white.
- **Frontend test discipline** — run the FULL vitest suite (not a single file) before claiming green ([[reference_frontend_full_suite_verification]]); `npx tsc --noEmit` must pass.
- **Tests run inside the frontend container:** `docker compose exec frontend npm test -- <path>` and `docker compose exec frontend npx tsc --noEmit`.
- **No AI attribution** in commits ([[feedback_no_ai_attribution]]).
- **Palette hex values (verbatim):**
  - Dark: cat-1 `#D4A64A`, cat-2 `#5FA8D3`, cat-3 `#4ade80`, cat-4 `#a78bfa`, cat-5 `#2dd4bf`, cat-6 `#f472b6`, cat-7 `#f59e0b`, cat-8 `#f87171`.
  - Light: cat-1 `#B88A2E`, cat-2 `#2f7fb0`, cat-3 `#16a34a`, cat-4 `#7c3aed`, cat-5 `#0d9488`, cat-6 `#db2777`, cat-7 `#d97706`, cat-8 `#dc2626`.

---

### Task 1: Categorical palette tokens in globals.css

**Files:**
- Modify: `frontend/app/globals.css` (dark block ~lines 14-61, light block ~lines 66+, chart tokens ~165-169)

**Interfaces:**
- Produces: CSS custom properties `--color-chart-1` … `--color-chart-8` resolving to the Brass-Harmony hues, theme-switched.

- [ ] **Step 1: Add the dark categorical scale.** In the dark `:root` (or `[data-theme="dark"]`) block, after the existing semantic tokens, add:
```css
  /* Categorical chart scale — Brass Harmony (W3). Chart-only; see DESIGN.md amendment. */
  --theme-cat-1: #D4A64A;
  --theme-cat-2: #5FA8D3;
  --theme-cat-3: #4ade80;
  --theme-cat-4: #a78bfa;
  --theme-cat-5: #2dd4bf;
  --theme-cat-6: #f472b6;
  --theme-cat-7: #f59e0b;
  --theme-cat-8: #f87171;
```

- [ ] **Step 2: Add the light categorical scale.** In the light theme block, add the deepened variants:
```css
  --theme-cat-1: #B88A2E;
  --theme-cat-2: #2f7fb0;
  --theme-cat-3: #16a34a;
  --theme-cat-4: #7c3aed;
  --theme-cat-5: #0d9488;
  --theme-cat-6: #db2777;
  --theme-cat-7: #d97706;
  --theme-cat-8: #dc2626;
```

- [ ] **Step 3: Re-alias the chart tokens.** Replace the existing `--color-chart-1..5` mapping (currently `var(--theme-info)` etc.) with:
```css
  --color-chart-1: var(--theme-cat-1);
  --color-chart-2: var(--theme-cat-2);
  --color-chart-3: var(--theme-cat-3);
  --color-chart-4: var(--theme-cat-4);
  --color-chart-5: var(--theme-cat-5);
  --color-chart-6: var(--theme-cat-6);
  --color-chart-7: var(--theme-cat-7);
  --color-chart-8: var(--theme-cat-8);
```

- [ ] **Step 4: Verify the token gate passes.** Run: `docker compose exec frontend bash frontend/scripts/check-design-tokens.sh` (or the repo's invocation path). Expected: exit 0 (the new `--color-chart-6..8` now exist, so any `bg-chart-*`/`var(--color-chart-*)` references resolve). If the script path differs, run via the CI npm script.

- [ ] **Step 5: Commit.**
```bash
git add frontend/app/globals.css
git commit -m "feat(reports): add 8-hue Brass Harmony categorical chart scale"
```

---

### Task 2: CHART_SERIES constant + consolidate the six arrays

**Files:**
- Modify: `frontend/lib/chart-colors.ts`
- Modify: `frontend/components/reports/widgets/BarWidgetChart.tsx`, `BarWidget.tsx`, `LineWidgetChart.tsx`, `AreaWidgetChart.tsx`, `StackedBarWidgetChart.tsx`, `PieWidgetChart.tsx`
- Test: `frontend/tests/lib/chart-colors.test.ts` (create)

**Interfaces:**
- Produces: `export const CHART_SERIES: readonly string[]` (8 entries, each `"var(--color-chart-N)"`) from `frontend/lib/chart-colors.ts`.
- Consumes: Task 1 tokens.

- [ ] **Step 1: Write the failing test.** Create `frontend/tests/lib/chart-colors.test.ts`:
```ts
import { describe, it, expect } from "vitest";
import { CHART_SERIES } from "@/lib/chart-colors";

describe("CHART_SERIES", () => {
  it("exposes 8 token-based categorical colors", () => {
    expect(CHART_SERIES).toHaveLength(8);
    CHART_SERIES.forEach((c, i) =>
      expect(c).toBe(`var(--color-chart-${i + 1})`)
    );
  });
});
```

- [ ] **Step 2: Run it, verify it fails.** Run: `docker compose exec frontend npm test -- tests/lib/chart-colors.test.ts`. Expected: FAIL (`CHART_SERIES` not exported).

- [ ] **Step 3: Add the constant.** In `frontend/lib/chart-colors.ts`, append:
```ts
// Categorical multi-series palette (Brass Harmony, W3). Single source of
// truth — every widget references this rather than a local array.
export const CHART_SERIES = [
  "var(--color-chart-1)",
  "var(--color-chart-2)",
  "var(--color-chart-3)",
  "var(--color-chart-4)",
  "var(--color-chart-5)",
  "var(--color-chart-6)",
  "var(--color-chart-7)",
  "var(--color-chart-8)",
] as const;
```
Also update the stale comment block (lines ~25-29) to describe `CHART_SERIES` and reference the DESIGN.md amendment instead of "brass conflicts with The One Brass Rule."

- [ ] **Step 4: Run test, verify it passes.** Run: `docker compose exec frontend npm test -- tests/lib/chart-colors.test.ts`. Expected: PASS.

- [ ] **Step 5: Replace the six local arrays.** In each of the six files, delete the local `const …COLORS = [ "var(--color-chart-1)", … ]` array and import + use `CHART_SERIES`:
```ts
import { CHART_SERIES } from "@/lib/chart-colors";
// …
fill={CHART_SERIES[i % CHART_SERIES.length]}   // or stroke=, as the file uses
```
Keep each file's existing index-wrap logic; only the source array changes.

- [ ] **Step 6: Typecheck + full suite.** Run: `docker compose exec frontend npx tsc --noEmit` then `docker compose exec frontend npm test`. Expected: both green (existing widget tests may assert specific colors — update any that hardcoded the old 5-array; see Task 6).

- [ ] **Step 7: Commit.**
```bash
git add frontend/lib/chart-colors.ts frontend/components/reports/widgets/
git commit -m "refactor(reports): single CHART_SERIES palette constant across widgets"
```

---

### Task 3: Donut + center total (PieWidgetChart)

**Files:**
- Modify: `frontend/components/reports/widgets/PieWidgetChart.tsx`
- Test: `frontend/tests/components/reports/pie-widget-chart.test.tsx` (create or extend)

**Interfaces:**
- Consumes: `CHART_SERIES`; `formatMeasureValue` + `reportCurrency` from `frontend/lib/reports/series.ts` (existing).

- [ ] **Step 1: Write the failing test.** Assert the chart renders a donut (inner radius) and a center total label. Example:
```tsx
import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import PieWidgetChart from "@/components/reports/widgets/PieWidgetChart";
// Build minimal props: rows summing to a known total, currency.
it("renders a center total for the donut", () => {
  render(<PieWidgetChart {/* rows: [{name:'A',value:300},{name:'B',value:200}] */} />);
  expect(screen.getByText(/500/)).toBeInTheDocument(); // formatted total in the hole
});
```
(Adjust prop names to the component's actual signature — read it first.)

- [ ] **Step 2: Run it, verify it fails.** Run: `docker compose exec frontend npm test -- tests/components/reports/pie-widget-chart.test.tsx`. Expected: FAIL (no center total).

- [ ] **Step 3: Implement donut + center total.** Add `innerRadius="58%"` (keep `outerRadius="80%"`) to `<Pie>`; compute `total = sum of resolved slice values` (after `top_n`/Other fold) and render it centered, e.g.:
```tsx
<text x="50%" y="50%" textAnchor="middle" dominantBaseline="central"
      className="fill-[var(--color-text-primary)]" style={{ fontSize: 15, fontWeight: 700 }}>
  {formatMeasureValue(total, format, currencyCode)}
</text>
```
(or a Recharts `<Label position="center" content={…}>`). Slice fills already come from `CHART_SERIES` after Task 2.

- [ ] **Step 4: Run test, verify it passes.** Expected: PASS.

- [ ] **Step 5: Typecheck.** Run: `docker compose exec frontend npx tsc --noEmit`. Expected: green.

- [ ] **Step 6: Commit.**
```bash
git add frontend/components/reports/widgets/PieWidgetChart.tsx frontend/tests/components/reports/pie-widget-chart.test.tsx
git commit -m "feat(reports): donut chart with center total"
```

---

### Task 4: Gradient smooth area (AreaWidgetChart)

**Files:**
- Modify: `frontend/components/reports/widgets/AreaWidgetChart.tsx`
- Test: `frontend/tests/components/reports/area-widget-chart.test.tsx` (create or extend)

- [ ] **Step 1: Write the failing test.** Assert each series gets a unique gradient def and monotone areas:
```tsx
it("emits a unique gradient def per series", () => {
  const { container } = render(<AreaWidgetChart {/* widgetId='w1', 2 series */} />);
  const grads = container.querySelectorAll("linearGradient");
  expect(grads.length).toBeGreaterThanOrEqual(2);
  // ids namespaced by widget id to avoid collisions
  expect(container.querySelector('linearGradient[id^="grad-w1-"]')).toBeTruthy();
});
```

- [ ] **Step 2: Run it, verify it fails.** Expected: FAIL.

- [ ] **Step 3: Implement.** For each series index `i`, emit:
```tsx
<defs>
  <linearGradient id={`grad-${widgetId}-${i}`} x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%" stopColor={CHART_SERIES[i % CHART_SERIES.length]} stopOpacity={0.5} />
    <stop offset="100%" stopColor={CHART_SERIES[i % CHART_SERIES.length]} stopOpacity={0.02} />
  </linearGradient>
</defs>
```
and on each `<Area type="monotone" stroke={CHART_SERIES[i % …]} fill={`url(#grad-${widgetId}-${i})`} />`. Thread the widget id into the component if not already present (use the existing widget id prop; if absent, accept an `idPrefix` prop and pass `widget.id` from `AreaWidget.tsx`).

- [ ] **Step 4: Run test, verify it passes.** Expected: PASS.

- [ ] **Step 5: Typecheck.** Expected: green.

- [ ] **Step 6: Commit.**
```bash
git add frontend/components/reports/widgets/AreaWidgetChart.tsx frontend/components/reports/widgets/AreaWidget.tsx frontend/tests/components/reports/area-widget-chart.test.tsx
git commit -m "feat(reports): gradient fill + smooth curve area charts"
```

---

### Task 5: Rounded palette bars (BarWidgetChart + StackedBarWidgetChart)

**Files:**
- Modify: `frontend/components/reports/widgets/BarWidgetChart.tsx`, `StackedBarWidgetChart.tsx`
- Test: extend `frontend/tests/components/reports/bar-widget-chart.test.tsx` (or create)

- [ ] **Step 1: Write the failing test.** Assert bars carry a corner radius:
```tsx
it("renders bars with rounded top corners", () => {
  const { container } = render(<BarWidgetChart {/* one series */} />);
  // Recharts renders <path> for bars; with radius the d-attr includes arcs.
  const bars = container.querySelectorAll(".recharts-bar-rectangle path");
  expect(bars.length).toBeGreaterThan(0);
  expect([...bars].some(p => /a/i.test(p.getAttribute("d") || ""))).toBe(true);
});
```
(If asserting on `d` is brittle, instead assert the `<Bar>` receives `radius` via a shallow prop check — read the component to pick the most stable assertion.)

- [ ] **Step 2: Run it, verify it fails.** Expected: FAIL.

- [ ] **Step 3: Implement.** Add `radius={[4, 4, 0, 0]}` to each `<Bar>` in both files. Confirm fills use `CHART_SERIES` (from Task 2). If `CartesianGrid` stroke is a hard color, set it to `var(--color-border)`.

- [ ] **Step 4: Run test, verify it passes.** Expected: PASS.

- [ ] **Step 5: Typecheck.** Expected: green.

- [ ] **Step 6: Commit.**
```bash
git add frontend/components/reports/widgets/BarWidgetChart.tsx frontend/components/reports/widgets/StackedBarWidgetChart.tsx frontend/tests/components/reports/bar-widget-chart.test.tsx
git commit -m "feat(reports): rounded palette bars"
```

---

### Task 6: Fix any existing tests that pinned the old 5-color palette

**Files:**
- Modify: any `frontend/tests/**` that asserts `var(--color-chart-3)` === grey or expects exactly 5 colors.

- [ ] **Step 1: Find them.** Run: `docker compose exec frontend grep -rn "color-chart-" tests/ || true`. Inspect each hit.
- [ ] **Step 2: Update assertions** to the new 8-hue expectations (or to reference `CHART_SERIES`).
- [ ] **Step 3: Full suite + typecheck.** Run: `docker compose exec frontend npx tsc --noEmit && docker compose exec frontend npm test`. Expected: all green.
- [ ] **Step 4: Commit.**
```bash
git add frontend/tests/
git commit -m "test(reports): update palette assertions to 8-hue scale"
```

---

### Task 7: DESIGN.md / DESIGN.json amendment + chart-colors comment

**Files:**
- Modify: `DESIGN.md` (~lines 204-206, ~337), `DESIGN.json`, `frontend/lib/chart-colors.ts` (comment, if not already done in Task 2)

- [ ] **Step 1: Amend the chart-palette paragraph (~line 204).** Replace the "Brass is intentionally excluded…" sentence with the carve-out: the categorical chart palette is an 8-hue scale (`chart-1..8`); it MAY include a gold series color because charts are data, not CTAs, and need many distinct hues. The One Brass Rule continues to govern chrome (CTAs, active sidebar item, focus ring). Note light/dark variants exist.

- [ ] **Step 2: Update the enumeration (~line 206)** from `chart-1..5` to the 8 named hues: gold, blue, green, violet, teal, pink, amber, coral; note coral/danger remains the over-budget signal via the `chartColor.over` semantic token.

- [ ] **Step 3: Update the Do bullet (~line 337)** from "keep brass out of charts" to "build chart series from `chart-1..8`; reserve the danger/coral token for the over-budget state."

- [ ] **Step 4: Sync DESIGN.json** chart-color list to the 8-hue scale.

- [ ] **Step 5: Verify token gate + full suite once more.** Run: `docker compose exec frontend bash frontend/scripts/check-design-tokens.sh && docker compose exec frontend npx tsc --noEmit && docker compose exec frontend npm test`. Expected: all green.

- [ ] **Step 6: Commit.**
```bash
git add DESIGN.md DESIGN.json frontend/lib/chart-colors.ts
git commit -m "docs(design): carve out chart-palette exception to The One Brass Rule"
```

---

## Self-review (done)

- **Spec coverage:** palette tokens (T1), consolidation (T2), donut+center total (T3), gradient smooth area (T4), rounded bars (T5), DESIGN.md amendment (T7), test fallout (T6). All PR1 spec items covered.
- **Placeholders:** none — each code step shows the code; test prop names flagged "read the component first" because the exact signatures must be confirmed against the actual files (acceptable: the assertion intent is concrete).
- **Type consistency:** `CHART_SERIES` name used consistently T2→T3/T4/T5; gradient id scheme `grad-${widgetId}-${i}` consistent in T4.

## Manual visual verification (after the build, before PR)

Charts must actually look beautiful, not just pass tests. With the dev stack running, open `/reports`, add one of each widget (pie/donut, area, bar, stacked bar, line) on a report, and screenshot in both dark and light themes. Confirm: distinct vibrant slices, donut center total legible, area gradient smooth, bars rounded. Iterate on hues/opacity if any series is muddy on navy or washed out on white.
