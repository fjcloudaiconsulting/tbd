// Shared chart color tokens. Centralized so Dashboard and the dedicated
// Budget / Forecast surfaces don't drift apart visually (D4, 2026-05-08).
//
// Each value is a CSS variable defined in `app/globals.css` so theme
// switches cascade automatically and we never embed raw palette hexes
// in component code.
//
// Semantic intent — keep these mappings stable across surfaces:
//   PLANNED   → accent (gold)            the user's intended commitment
//   ACTUAL    → success (green)          settled spending under plan
//   SPENT     → accent (gold)            same gold as PLANNED, intentional
//   WATCH     → text-secondary (neutral) 80%-100% utilization
//   OVER      → danger (red)             over plan / over budget
//   REMAINING → border (neutral track)   remaining headroom in a stack
export const chartColor = {
  planned: "var(--color-accent)",
  actual: "var(--color-success)",
  spent: "var(--color-accent)",
  watch: "var(--color-text-secondary)",
  over: "var(--color-danger)",
  remaining: "var(--color-border)",
  axisTick: "var(--color-text-secondary)",
} as const;

// Fill for a budget's spent bar. Keyed on `over_budget`, not
// `percent_used > 100`: percent_used is 0 for a 0-amount budget (TBD-556).
export function budgetBarFill(b: { percent_used: number; over_budget: boolean }): string {
  if (b.over_budget) return chartColor.over;
  return b.percent_used > 80 ? chartColor.watch : chartColor.spent;
}

// Categorical multi-series palette for report widgets (W3 visual refresh).
// Single source of truth — every widget imports CHART_SERIES rather than
// maintaining its own local array. Palette expanded to 8 tokens as part of
// W3; since TBD-429 the hues contain no brass and are disjoint from the
// status tokens (docs/design/DESIGN.md, Data Visualization), enforced by
// tests/lib/chart-palette-invariant.test.ts. Tokens defined in `app/globals.css` so theme-switches cascade
// automatically; never embed raw hex here.
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
