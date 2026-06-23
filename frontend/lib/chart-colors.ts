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

// Categorical multi-series palette for report widgets (W3 visual refresh).
// Single source of truth — every widget imports CHART_SERIES rather than
// maintaining its own local array. Palette expanded to 8 tokens as part of
// W3; the DESIGN.md chart-palette exception documents why these colors are
// exempt from The One Brass Rule (they are data-series hues, not brand
// surfaces). Tokens defined in `app/globals.css` so theme-switches cascade
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
