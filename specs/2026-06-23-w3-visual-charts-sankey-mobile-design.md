# W3 — Visual/color chart refresh + Cash-flow Sankey + Reports mobile

**Date:** 2026-06-23
**Status:** Approved (brainstorm complete)
**Workstream:** W3 of the 2026-06-22 product re-prioritization (`specs/2026-06-22-product-reprioritization.md`, [[project_reprioritization_2026_06_22]])
**Motivation:** Operator reviewed Monarch Money's UI ("jealous of how beautiful it looks"). Today's chart "palette" is five muted theme tokens — two of them are literally grey text colors (`--color-chart-3 = text-secondary`, `--color-chart-4 = text-muted`). Charts read flat and grey. This wave makes them colorful, modern, and adds Monarch's signature cash-flow Sankey, plus a mobile read-only polish pass.

## Locked decisions (from brainstorm)

1. **Scope:** full W3 — color/visual refresh + Sankey + mobile — split across **3 PRs**.
2. **Palette:** direction **A "Brass Harmony"** — a warm, brand-anchored 8-hue categorical scale.
3. **Chart styling:** ship all three upgrades — **donut + center total**, **gradient smooth area**, **rounded palette bars**.
4. **Sankey structure:** **A** — income sources → **Income** hub → spending categories (+ Income→Savings when income > expense). Lives as a **Reports widget** on the canvas (reuses the source registry, shared date bar, per-widget filter chips).
5. **Mobile:** **read-only usability** pass only (no touch editing; that overlaps W4 gridstack).
6. **Brass rule:** **amend DESIGN.md** to carve out an explicit chart-palette exception. Charts aren't CTAs and legitimately need many distinct hues; one of them being gold does not dilute the "where do I click" signal. We keep the approved look and make the design system self-consistent.

## Design-system constraints that still hold

- **No Off-Token Rule** — every color must be a `--color-*` token defined in `frontend/app/globals.css`; raw Tailwind palette colors and bare hex are CI-blocked by `frontend/scripts/check-design-tokens.sh`. New chart hues are added as theme tokens, referenced as `var(--color-chart-N)`.
- **The One Brass Rule** — still governs *chrome* (CTAs, active sidebar item, focus ring). The amendment is narrow: it applies only to the categorical chart series palette.
- **Sidebar-Always-Navy**, **Brand-Surface Lock** — untouched.
- **WCAG 2.2 AA** — color is never the only signal; charts keep labels/legends/tooltips. Light-theme hues are deepened for contrast on white.

---

## PR1 — Palette + chart styling refresh (M)

### 1. Categorical palette tokens

Introduce a dedicated 8-hue categorical scale as first-class theme tokens, defined in **both** the dark (default) and light blocks of `frontend/app/globals.css`. This stops overloading semantic tokens (`info`/`success`/`text-*`) for categorical series.

**Dark theme (on Ledger Navy `#0B1F3A`):**

| token | hue | hex |
|---|---|---|
| `--theme-cat-1` | gold | `#D4A64A` |
| `--theme-cat-2` | blue | `#5FA8D3` |
| `--theme-cat-3` | green | `#4ade80` |
| `--theme-cat-4` | violet | `#a78bfa` |
| `--theme-cat-5` | teal | `#2dd4bf` |
| `--theme-cat-6` | pink | `#f472b6` |
| `--theme-cat-7` | amber | `#f59e0b` |
| `--theme-cat-8` | coral | `#f87171` |

**Light theme (on white, deepened for contrast):**

| token | hue | hex |
|---|---|---|
| `--theme-cat-1` | gold | `#B88A2E` |
| `--theme-cat-2` | blue | `#2f7fb0` |
| `--theme-cat-3` | green | `#16a34a` |
| `--theme-cat-4` | violet | `#7c3aed` |
| `--theme-cat-5` | teal | `#0d9488` |
| `--theme-cat-6` | pink | `#db2777` |
| `--theme-cat-7` | amber | `#d97706` |
| `--theme-cat-8` | coral | `#dc2626` |

Alias the public chart tokens to the scale:

```css
--color-chart-1: var(--theme-cat-1);
/* … through … */
--color-chart-8: var(--theme-cat-8);
```

**Preserve over-budget semantics.** Today `--color-chart-5` doubled as the over-budget coral and code paths in `frontend/lib/reports/chart-series-tooltip.ts` reference `chartColor.over` (= `--color-danger`). `chartColor.over` already points at `--color-danger`, not `--color-chart-5`, so the budget/over semantics are unaffected by re-indexing the categorical scale. Verify no consumer relies on `chart-5 === coral` positionally; the budget surfaces use the `chartColor.*` semantic map, not the categorical array.

### 2. Consolidate the palette into one constant

Today six files each hardcode a 5-element `var(--color-chart-1..5)` array:
`BarWidgetChart.tsx`, `BarWidget.tsx` (legend), `LineWidgetChart.tsx`, `AreaWidgetChart.tsx`, `StackedBarWidgetChart.tsx`, `PieWidgetChart.tsx`.

Add one exported constant in `frontend/lib/chart-colors.ts`:

```ts
export const CHART_SERIES = [
  "var(--color-chart-1)", "var(--color-chart-2)", "var(--color-chart-3)",
  "var(--color-chart-4)", "var(--color-chart-5)", "var(--color-chart-6)",
  "var(--color-chart-7)", "var(--color-chart-8)",
] as const;
```

Replace all six local arrays with `CHART_SERIES`. Series color = `CHART_SERIES[i % CHART_SERIES.length]` (wrap, as today). This is the only place the palette length lives going forward.

### 3. Three visual upgrades

**Donut + center total** (`PieWidgetChart.tsx`):
- Add `innerRadius` to the Recharts `<Pie>` (e.g. `innerRadius="58%" outerRadius="80%"`), turning pie → donut.
- Render the total (sum of resolved slice values, after the `top_n`/"Other" fold) centered in the hole, formatted with the existing `formatMeasureValue` + org currency (`reportCurrency`, already shipped #455). Use a Recharts `<Label position="center">` or a centered SVG `<text>`.
- Keep `top_n` "Other" bucketing and existing tooltip/legend behavior.

**Gradient smooth area** (`AreaWidgetChart.tsx`):
- Per series, emit a `<defs><linearGradient>` (vertical: token color at ~0.5 alpha → ~0.02 alpha). Set `<Area fill="url(#grad-<id>-<i>)" stroke="var(--color-chart-N)" type="monotone">`.
- Gradient ids must be unique per widget instance + series (prefix with the widget id) to avoid `<defs>` collisions when multiple area widgets render on one canvas.
- Keep `stacked` behavior.

**Rounded palette bars** (`BarWidgetChart.tsx`, `StackedBarWidgetChart.tsx`):
- `<Bar radius={[4,4,0,0]}>`. For stacked bars, only the top-most segment should round visually — acceptable to round every segment's top (Recharts limitation); keep simple: apply `[4,4,0,0]` to the series and accept the standard stacked look.
- Lighten `CartesianGrid` stroke to a subtle border token if not already.

### 4. DESIGN.md / DESIGN.json amendment

Edit `DESIGN.md`:
- **Line ~204** — replace "Brass is intentionally excluded…" with the carve-out: the categorical chart palette is an 8-hue scale (`chart-1..8`) that **may** include a gold series color; The One Brass Rule governs chrome (CTAs/active/focus), not data series. Note light/dark variants.
- **Line ~206** — update the `chart-1..5` enumeration to `chart-1..8` with the new hue names.
- **Line ~337 (Do)** — change "keep brass out of charts" to reflect the scale (build series from `chart-1..8`; reserve the `over`/danger token for over-budget).
- Update `DESIGN.json` sidecar chart-color list to match.
- Update the comment block in `frontend/lib/chart-colors.ts` (currently says the brass-inclusive `categoricalColors` helper was removed because it "conflicts with The One Brass Rule") to reflect the new `CHART_SERIES` constant + the amendment.

### PR1 acceptance
- All chart widgets render the 8-hue palette in both themes; no grey series.
- Pie renders as a donut with a correct, currency-formatted center total.
- Area renders gradient + smooth; bars render rounded + colored.
- `check-design-tokens.sh` passes (all new tokens exist in `globals.css`; no bare hex in components).
- `tsc --noEmit` + full vitest suite green; existing widget tests updated for the new color count/donut.

---

## PR2 — Cash-flow Sankey widget (L)

### 1. Dependency
Add `@nivo/sankey` + `@nivo/core` (MIT, React-19 compatible, SVG, verified June 2026 — [[project_reprioritization_2026_06_22]]). Render via `next/dynamic(() => import(...), { ssr:false })` (client-only), consistent with the chart canvas. Nivo accepts `var(--color-chart-*)` strings as SVG fills, preserving No-Off-Token.

### 2. Frontend widget wiring
Thread a new kind `"sankey"` through, mirroring the existing 8 kinds:
- `frontend/lib/reports/types.ts` — add to `WidgetType` union; add `SankeyConfig`, `SankeyWidget` alias, add to `Widget` union.
- `SankeyConfig` shape (minimal — structure A is fixed):
  ```ts
  interface SankeyConfig {
    dataset: "transactions";
    measure: Measure;            // sum of amount
    filters?: WidgetFilters;     // reuses canvas date cascade + per-widget chips
    spending_granularity?: "category" | "category_master"; // right-side detail, default "category"
    top_n?: number;              // fold small spending categories into "Other"
  }
  ```
- `frontend/components/reports/widgetKit.tsx` — add `emptySankey(id)` factory + `case "sankey"` in `renderWidgetByType`.
- `frontend/components/reports/WidgetPicker.tsx` — add to the "Categories" (or a new "Flow") group with icon + description.
- New `frontend/components/reports/widgets/SankeyWidget.tsx` (data + shell) and `SankeyWidgetChart.tsx` (the Nivo `ResponsiveSankey`).
- Color: feed Nivo a `colors` array = `CHART_SERIES`; nodes colored by index.

### 3. Backend endpoint
New `POST /api/v1/reports/query/sankey` in `backend/app/routers/reports.py` (auth via `get_current_user`, org-scoped).
- Request schema (`backend/app/schemas/reports_query.py`): `dataset` (transactions), `measure`, `filters`, `spending_granularity`, `top_n`.
- Response: `{ links: [{ source: str, target: str, value: number }], meta }`. Nivo derives nodes from links; optionally also return `nodes` for stable ordering/colors.
- Implementation reuses the transactions filter/date/org-scoping machinery in `backend/app/services/reports_query_service.py` (cash-basis via `effective_period_date_expr()` — [[reference_effective_period_date_cash_basis]]). Two aggregations under the same filter set:
  - **Income side:** `txn_type = income`, group by category → links `category → "Income"`.
  - **Spending side:** `txn_type = expense`, group by `category` (or `category_master` per `spending_granularity`) → links `"Income" → category`. Fold beyond `top_n` into `"Income" → "Other"`.
  - **Savings:** if `Σincome > Σexpense`, add link `"Income" → "Savings"` = the difference. (If expense > income, omit; do not render negative flow.)
- Validation: reject cycles / empty result gracefully (return `{links: []}` → widget shows an empty state).

### 4. Edge cases
- No income, only expense → no hub inflow; render expense links from a synthetic "Income" node sized to total expense, or show empty state with guidance. **Decision:** show empty state ("No income in this period to chart cash flow") to avoid a misleading diagram.
- Transfers excluded (only income/expense participate).
- Multi-currency org: same documented trade-off as #455 — label uses the org's primary currency.

### PR2 acceptance
- Sankey widget addable from the picker, renders income→hub→categories with palette colors, respects the shared date range + per-widget filters.
- Center/savings link correct; empty states correct.
- Endpoint org-scoped + cash-basis; backend tests cover income/expense/savings/empty.
- `tsc` + vitest + backend pytest green.

---

## PR3 — Reports mobile read-only pass (M)

Reports already render read-only on mobile (#382). This is polish, not new capability.
- **Canvas reflow** (`Canvas.tsx`): on small viewports, widgets stack to a single column with sensible min-heights so charts aren't crushed (Recharts/Nivo `ResponsiveContainer` already handle width). Verify the `react-grid-layout` breakpoint behavior; if it doesn't already collapse to 1 col on mobile, force a single-column read-only layout below the mobile breakpoint.
- **Date bar + filter chips** (`CanvasFiltersBar.tsx`, `WidgetFilterChips.tsx`): `flex-wrap` so they wrap instead of causing page-level horizontal scroll.
- **Legibility:** tooltip/axis/legend font sizes remain readable; consider thinning dense time-axis ticks on narrow widths.
- **Tables:** horizontal overflow contained inside the widget card (`overflow-x-auto` on the table wrapper), never the page.
- **No page-level horizontal scroll** at 360–414px widths.
- Touch targets on any read-only affordances (view toggles, chip removal if shown) meet `min-h-[44px]` per DESIGN.md.

### PR3 acceptance
- At 360/390/414px: no page horizontal scroll; widgets single-column, legible; date bar/chips wrap; tables scroll within their card.
- No editing affordances regress; desktop layout unchanged.
- `tsc` + vitest green.

---

## Sequencing & dependencies
- **PR1** — independent, ship first (establishes the palette tokens + `CHART_SERIES`).
- **PR2** — depends on PR1's tokens for Sankey coloring.
- **PR3** — independent; can run in parallel with PR2.

## Out of scope (explicit)
- Touch drag/resize/add widgets on mobile → W4 (gridstack migration).
- Dashboard placement of the Sankey (Reports-widget only this wave).
- New chart types beyond Sankey; net-worth source (Reports Phase 6, decision-gated).
- Founders referral / inactivity-revoke (W2, payments wave).

## Review plan
Each PR through the established fleet review (N dimensions → per-finding skeptic verify → synthesis), `tsc --noEmit` + full vitest suite + backend pytest on an isolated compose project, design-token gate, then merge.
